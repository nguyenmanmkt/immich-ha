import ast
import copy
import json
import subprocess
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

try:import pwd
except ModuleNotFoundError:sys.modules.setdefault('pwd',types.SimpleNamespace(getpwnam=lambda _:types.SimpleNamespace(pw_uid=999,pw_gid=1000)))
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'node'))
import witness_bootstrap as w
import cluster_service as service
import quorum_stage as stage
from quorum_lock import validate_config
from witness_router import validate as validate_router
from local_gateway import render


class WitnessInstallerTests(unittest.TestCase):
    def test_local_catalog_loads_without_paramiko_or_personal_configuration(self):
        script='''import builtins,sys,types
try:import pwd
except ModuleNotFoundError:sys.modules['pwd']=types.SimpleNamespace()
sys.path[:0]=[sys.argv[1],sys.argv[1]+'/node']
original=builtins.__import__
def guard(name,*args,**kwargs):
 if name=='paramiko' or name.startswith('paramiko.'):raise AssertionError('Local installer must not load SSH transport')
 return original(name,*args,**kwargs)
builtins.__import__=guard
import cluster_service,witness_bootstrap,streaming_setup
for name in ('quorum-enforce','quorum-fallback'):witness_bootstrap.module(name)
'''
        subprocess.run([sys.executable,'-I','-c',script,str(ROOT)],check=True,capture_output=True)

    def plan(self,mode='lan'):
        return {'version':1,'engine':'etcd','networkMode':mode,'clusterId':'12345678-abcd-abcd-abcd-123456789abc',
            'grantIntegrationEnabled':False,'funnelEnabled':False,
            'members':{n:{'ip':('192.168.1.' if mode=='lan' else '100.64.0.')+str(i+1),'clientPort':2379,'peerPort':2380} for i,n in enumerate(w.NODES)}}

    def test_lan_quorum_mtls_configuration_does_not_require_tailscale(self):
        plan=self.plan();stage.validate_plan(plan)
        self.assertEqual(stage.configuration(plan,'witness')['listen-peer-urls'],'https://192.168.1.3:2380')
        self.assertNotIn('tailscaled',stage.service('lan'))
        with tempfile.TemporaryDirectory() as tmp:
            pki=stage.certificates(plan,Path(tmp)/'pki')
            from cryptography import x509
            cert=x509.load_pem_x509_certificate((pki/'witness-server.crt').read_bytes())
            self.assertEqual(str(cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress)[0]),'192.168.1.3')

    def test_lan_agent_router_and_trusted_scheme_stay_private(self):
        plan=self.plan();args={'plan':plan,'node':'primary','networkMode':'lan'}
        cfg=w.config(args);validate_config({**cfg,'mode':'shadow'})
        router={'version':1,'clusterId':plan['clusterId'],'networkMode':'lan','endpoints':cfg['endpoints'],
            'nodes':{n:{'ip':plan['members'][n]['ip'],'port':8090} for n in ('primary','backup')}}
        validate_router(router)
        text=render((ROOT/'node/immich-ha-local.nginx.conf').read_text(),8090,'a'*48,['192.168.1.3'],'lan')
        self.assertIn('192.168.1.3:https',text)
        for bad in ['8.8.8.8','127.0.0.1','0.0.0.0','100.64.0.3']:
            with self.assertRaises(ValueError):render((ROOT/'node/immich-ha-local.nginx.conf').read_text(),8090,'a'*48,[bad],'lan')
        bad=copy.deepcopy(router);bad['nodes']['backup']['ip']='8.8.8.8'
        with self.assertRaises(ValueError):validate_router(bad)
        with self.assertRaises(ValueError):validate_config({**cfg,'mode':'shadow','networkMode':'tailscale'})

    def test_action_identity_is_pinned_and_cannot_restage_or_execute_unknown_code(self):
        plan=self.plan();local={'node':'primary','ip':'192.168.1.1','networkMode':'lan'}
        args={'plan':plan,'setupId':plan['clusterId'],'node':'primary','networkMode':'lan','owner':'backup','epoch':5}
        with tempfile.TemporaryDirectory() as tmp,patch.object(w,'APP',Path(tmp)):
            with self.assertRaisesRegex(RuntimeError,'Inspect'):w.pin_command({'action':'witness-install','args':args},local)
            w.pin_command({'action':'witness-inspect','args':args},local)
            with self.assertRaisesRegex(RuntimeError,'retained'):w.pin_command({'action':'witness-install','args':{**args,'epoch':6}},local)
            with self.assertRaisesRegex(RuntimeError,'Unsupported'):w.execute({'action':'witness-shell','args':args},local)

    def test_receipts_route_only_packaged_extension_actions_and_replay_no_changes(self):
        command={'id':'12345678-abcd-abcd-abcd-123456789abc-0-0','action':'witness-inspect','args':{}}
        with tempfile.TemporaryDirectory() as tmp,patch.object(service,'APP',Path(tmp)),patch.object(w,'execute',return_value={'verified':True}) as execute:
            self.assertEqual(service.run_local(command,{})['status'],'succeeded')
            self.assertEqual(service.run_local(command,{})['status'],'succeeded');execute.assert_called_once()
            directory=Path(tmp)/'.install/receipts';other={**command,'id':command['id'].replace('-0-0','-1-0')}
            (directory/(other['id']+'.started')).write_text('started')
            self.assertEqual(service.run_local(other,{})['status'],'failed');execute.assert_called_once()

    def test_private_failure_details_never_enter_worker_receipt(self):
        command={'id':'12345678-abcd-abcd-abcd-123456789abc-0-0','action':'witness-pki','args':{}}
        with tempfile.TemporaryDirectory() as tmp,patch.object(service,'APP',Path(tmp)),patch.object(w,'execute',side_effect=RuntimeError('private-certificate-test')):
            result=service.run_local(command,{})
            self.assertNotIn('private-certificate-test',json.dumps(result))
            self.assertIn('private-certificate-test',(Path(tmp)/'.install/receipts'/(command['id']+'.error')).read_text())

    def test_common_funnel_toggle_changes_only_owned_listener_and_confirms_revision(self):
        dns='witness.example.ts.net';address=dns+':443'
        before={'TCP':{'443':{'HTTPS':True},'8443':{'HTTPS':True}},
            'Web':{address:{'Handlers':{'/':{'Proxy':'http://127.0.0.1:8090'}}},dns+':8443':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:9000'}}}},
            'AllowFunnel':{address:True}}
        after=copy.deepcopy(before)
        for key,item in [('TCP','443'),('Web',address),('AllowFunnel',address)]:
            after[key].pop(item)
            if not after[key]:after.pop(key)
        for changed in (False,True):
            result=copy.deepcopy(after)
            if changed:result['Web'][dns+':8443']['Handlers']['/']['Proxy']='http://127.0.0.1:9999'
            with patch.object(w.Path,'exists',return_value=True),patch.object(w.Path,'read_text',return_value=json.dumps({'node':'witness','dnsName':dns,'port':443,'gatewayPort':8090})),patch.object(w,'run',side_effect=[json.dumps(before),'',json.dumps(result)]) as run:
                report=w.reconcile_funnel({'enabled':False,'revision':7})
                self.assertEqual(report['revision'],7)
                if changed:self.assertIn('error',report)
                else:self.assertIs(report['enabled'],False);self.assertNotIn('error',report)
                self.assertEqual(run.call_args_list[1].args[0],['tailscale','funnel','--https=443','off'])

    def test_fixed_payloads_and_wrappers_compile_without_live_import_side_effects(self):
        for name in ('quorum-enforce','quorum-fallback'):
            module=w.module(name)
            for key in ('INSTALL','ENABLE','CHECK'):
                if hasattr(module,key):compile(getattr(module,key),name+'-'+key,'exec')
        for code in (stage.PREPARE,stage.START,stage.CHECK):compile(code,'stage','exec')
        for name in ('node/witness_bootstrap.py','tools/install-witness-local.py'):
            tree=ast.parse((ROOT/name).read_text(encoding='utf-8'))
            imports=[n.module for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
            self.assertFalse(any(n in ('paramiko','ssh_nodes','deployment') for n in imports))
