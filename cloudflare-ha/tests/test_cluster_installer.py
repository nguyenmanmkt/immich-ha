import ast
import importlib.util
import subprocess
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock,patch

try:import pwd
except ModuleNotFoundError:sys.modules.setdefault('pwd',types.SimpleNamespace(getpwnam=lambda _:types.SimpleNamespace(pw_uid=999,pw_gid=1000)))
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'node'))
import cluster_bootstrap as bootstrap
import cluster_service as service
import bootstrap_cloudflare as cf


class ClusterInstallerTests(unittest.TestCase):
    def test_local_payload_successful_exit_keeps_its_receipt_but_failure_does_not(self):
        self.assertEqual(bootstrap.payload("print('{\"prepared\":true}');raise SystemExit(0)",{}),{'prepared':True})
        with self.assertRaisesRegex(RuntimeError,'unsuccessfully'):bootstrap.payload('raise SystemExit(1)',{})
        with self.assertRaises(ValueError):bootstrap.payload('raise SystemExit(0)',{})

    def test_uninitialized_native_schema_stops_inspection_before_node_install(self):
        local={'node':'backup','ip':'192.168.1.20','networkMode':'lan'}
        with patch.object(bootstrap,'run',return_value='[{"addr_info":[{"local":"192.168.1.20"}]}]'),patch.object(bootstrap.os.path,'ismount',return_value=True),patch.object(bootstrap,'native_env',return_value={'DB_USERNAME':'immich'}),patch.object(bootstrap,'media_inventory',return_value={'files':0,'bytes':0}),patch.object(bootstrap,'native_sql',return_value='f') as sql:
            with self.assertRaisesRegex(RuntimeError,'schema is not initialized'):bootstrap.inspect(local)
            sql.assert_called_once()

    def test_bootstrap_service_loads_its_catalog_from_an_isolated_process(self):
        script="""import importlib.util,sys,types
try:import pwd
except ModuleNotFoundError:sys.modules['pwd']=types.SimpleNamespace()
spec=importlib.util.spec_from_file_location('isolated_bootstrap_service',sys.argv[1])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
import pg_stream
assert str(pg_stream.replication_address('192.168.1.10','lan'))=='192.168.1.10'
"""
        subprocess.run([sys.executable,'-I','-c',script,str(ROOT/'node/cluster_service.py')],check=True,capture_output=True)
    def test_cloudflare_provisioning_preserves_owned_resources_and_scopes_origin_auth(self):
        project={'workerName':'immich-ha','publicHost':'immich.example.org','adminHost':'ha.example.org',
          'origins':{'primary':'https://a.example.org','backup':'https://b.example.org'},'access':{'emails':['admin@example.org']}}
        calls=[]
        def api(path,method='GET',body=None,**kwargs):
            calls.append((path,method,body))
            if path.endswith('/access/organizations'):return {'auth_domain':'example.cloudflareaccess.com'}
            if path.endswith('/identity_providers'):return [{'id':'otp','type':'onetimepin'}]
            if path.endswith('/service_tokens'):return {'id':'service','client_id':'test.access','client_secret':'test_origin_secret_not_real_12345678901234567890'}
            if path.endswith('/access/apps'):return {'id':'app-'+body['domain'],'aud':'app-audience'}
            if path.endswith('/cfd_tunnel'):return {'id':'tunnel-'+body['name'],'token':'tunnel-token-not-real'}
            return {'id':'new-resource'}
        state={};save=Mock()
        tunnels,credentials=cf.provision(api,project,'account','zone',state,save)
        self.assertEqual(set(tunnels),{'primary','backup'});self.assertTrue(credentials['ORIGIN_ACCESS_CLIENT_ID'].endswith('.access'))
        policies=[body for path,method,body in calls if path.endswith('/policies')]
        self.assertEqual([p['decision'] for p in policies],['allow','bypass','non_identity','non_identity'])
        self.assertEqual(policies[2]['include'],[{'service_token':{'token_id':'service'}}])
        posts=sum(method=='POST' for _,method,_ in calls)
        cf.provision(api,project,'account','zone',state,save)
        self.assertEqual(sum(method=='POST' for _,method,_ in calls),posts)

    def test_interrupted_local_action_never_reexecutes_and_completed_receipts_replay(self):
        command={'id':'12345678-abcd-abcd-abcd-123456789abc-0-0','action':'inspect','args':{}}
        with tempfile.TemporaryDirectory() as tmp,patch.object(service,'APP',Path(tmp)),patch.object(service,'execute',return_value={'ok':True}) as execute:
            first=service.run_local(command,{})
            self.assertEqual(first,service.run_local(command,{}));execute.assert_called_once()
            other=dict(command,id='12345678-abcd-abcd-abcd-123456789abc-1-0')
            directory=Path(tmp)/'.install/receipts';(directory/(other['id']+'.started')).write_text('started')
            self.assertEqual(service.run_local(other,{})['status'],'failed');execute.assert_called_once()

    def test_action_catalog_refuses_unknown_code_and_wrong_identity(self):
        local={'id':'12345678-abcd-abcd-abcd-123456789abc','node':'primary','networkMode':'lan'}
        command={'action':'shell','args':{'setupId':local['id'],'node':'primary','networkMode':'lan'}}
        with self.assertRaisesRegex(RuntimeError,'Unsupported'):bootstrap.execute(command,local)
        command['args']['node']='backup'
        with self.assertRaisesRegex(RuntimeError,'identity'):bootstrap.execute(command,local)

    def test_cloudflare_preflight_does_not_overwrite_existing_dns(self):
        project={'workerName':'immich-ha','publicHost':'immich.example.org','adminHost':'ha.example.org','origins':{'primary':'https://a.example.org','backup':'https://b.example.org'}}
        def api(path,*args,**kw):
            if path=='/user/tokens/verify':return {'status':'active'}
            if path=='/zones/zone':return {'account':{'id':'account'},'name':'example.org','status':'active'}
            return [{'id':'existing-dns'}]
        with self.assertRaisesRegex(RuntimeError,'already has DNS'):cf.preflight(api,project,'account','zone')

    def test_installer_scripts_compile_and_no_ssh_transport_is_imported(self):
        for name in ('node/cluster_bootstrap.py','node/cluster_service.py','tools/install-cluster.py','bootstrap_cloudflare.py'):
            tree=ast.parse((ROOT/name).read_text(encoding='utf-8'))
            imports=[n.module for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
            self.assertFalse(any(x in ('paramiko','ssh_nodes','deployment') for x in imports))

    def test_new_installer_refuses_existing_ha_before_cloudflare_calls(self):
        spec=importlib.util.spec_from_file_location('cluster_installer_cli',ROOT/'tools/install-cluster.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with patch.object(module,'require_fresh',side_effect=RuntimeError('Existing HA retained')),patch.object(module,'API') as api:
            with self.assertRaisesRegex(RuntimeError,'Existing HA'):module.initialize()
            api.assert_not_called()
