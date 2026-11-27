import base64
import copy
import importlib.util
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock
import uuid

from node import witness_router as r
import witness_gateway_setup as w
import local_gateway as g


class WitnessRouterTests(unittest.TestCase):
    def setUp(self):
        self.cluster=str(uuid.uuid4())
        self.cfg={'version':1,'clusterId':self.cluster,'nodes':{'primary':{'ip':'100.64.0.1','port':8090},'backup':{'ip':'100.64.0.2','port':8090}},
                  'endpoints':['https://100.64.0.'+str(i)+':2379' for i in (1,2,3)]}
        self.record={'owner':'backup','epoch':14,'session':'persistent_session_12345','authority':'cloudflare'}

    def result(self,record=None):
        value=self.record if record is None else record
        encoded=base64.b64encode(json.dumps(value).encode()).decode()
        return {'responses':[{'response_range':{'kvs':[{'value':encoded,'lease':'0'}]}},
                             {'response_range':{'kvs':[{'value':encoded,'lease':'123'}]}}]}

    def test_reader_payload_only_reads_two_exact_production_keys(self):
        payload=r.read_payload(self.cluster)
        self.assertEqual(payload['compare'],[]);self.assertEqual(payload['failure'],[])
        self.assertEqual(len(payload['success']),2)
        for op,name in zip(payload['success'],('generation','active')):
            self.assertEqual(set(op),{'request_range'})
            self.assertIs(op['request_range']['serializable'],False)
            self.assertEqual(base64.b64decode(op['request_range']['key']).decode(),'/immich-ha/'+self.cluster+'/control/admission/'+name)

    def test_routes_cf_fallback_and_handoff_without_becoming_coordinator(self):
        for authority in ('cloudflare','fallback','handoff'):
            record={**self.record,'authority':authority}
            self.assertEqual(r.select_route(self.cfg,self.result(record)),{'owner':'backup','epoch':14})
        record={**self.record,'owner':'primary','epoch':15}
        self.assertEqual(r.select_route(self.cfg,self.result(record)),{'owner':'primary','epoch':15})

    def test_expired_frozen_or_inconsistent_authority_never_routes(self):
        for change in ({'released':True},{'localBlocked':True},{'owner':'other'},{'epoch':0},{'epoch':True},{'session':'short'},{'authority':'invalid'}):
            with self.assertRaises(RuntimeError):r.select_route(self.cfg,self.result({**self.record,**change}))
        for mode in ('expired','missing','inconsistent'):
            value=self.result()
            if mode=='expired':value['responses'][1]['response_range']['kvs'][0]['lease']='0'
            elif mode=='missing':value['responses'][1]['response_range']['kvs']=[]
            else:value['responses'][1]['response_range']['kvs'][0]['value']=base64.b64encode(json.dumps({**self.record,'epoch':15}).encode()).decode()
            with self.assertRaises(RuntimeError):r.select_route(self.cfg,value)

    def test_cache_is_bounded_and_never_serves_stale_after_failed_refresh(self):
        reader=Mock();reader.read.return_value={'owner':'backup','epoch':14};now=[100]
        resolver=r.Resolver(reader,clock=lambda:now[0])
        self.assertEqual(resolver.resolve()['owner'],'backup')
        now[0]=100.4;resolver.resolve();self.assertEqual(reader.read.call_count,1)
        now[0]=100.6;reader.read.side_effect=RuntimeError('no quorum')
        with self.assertRaises(RuntimeError):resolver.resolve()
        self.assertIsNone(resolver.value)
        reader.read.side_effect=None;reader.read.return_value={'owner':'primary','epoch':15}
        self.assertEqual(resolver.resolve()['owner'],'primary')

    def test_concurrent_requests_coalesce_and_return_independent_results(self):
        reader=Mock();reader.read.return_value={'owner':'backup','epoch':14}
        resolver=r.Resolver(reader,clock=lambda:100);values=[]
        threads=[threading.Thread(target=lambda:values.append(resolver.resolve())) for _ in range(16)]
        for t in threads:t.start()
        for t in threads:t.join(timeout=2)
        self.assertEqual(reader.read.call_count,1);self.assertEqual(len(values),16)
        values[0]['owner']='changed';self.assertEqual(resolver.resolve()['owner'],'backup')

    def test_route_handler_denies_when_quorum_cannot_be_confirmed(self):
        resolver=Mock();resolver.resolve.side_effect=RuntimeError('unavailable')
        handler=object.__new__(r.handler(resolver));handler.path='/route'
        handler.send_response=Mock();handler.send_header=Mock();handler.end_headers=Mock()
        handler.do_GET();handler.send_response.assert_called_once_with(403)
        self.assertFalse(any(call.args[0]=='X-Immich-HA-Node' for call in handler.send_header.call_args_list))

    def test_no_arbitrary_targets_or_bypass_ports(self):
        r.validate(self.cfg)
        for port in (2283,2379,5433,8080,9081):
            value=copy.deepcopy(self.cfg);value['nodes']['backup']['port']=port
            with self.assertRaises(ValueError):r.validate(value)
        value=copy.deepcopy(self.cfg);value['nodes']['backup']['ip']='127.0.0.1'
        with self.assertRaises(ValueError):r.validate(value)
        value=copy.deepcopy(self.cfg);value['endpoints'][0]='https://example.com:2379'
        with self.assertRaises(ValueError):r.validate(value)

    def test_nginx_never_replays_requests_and_uses_fixed_targets(self):
        text=(Path(__file__).parents[1]/'node/immich-ha-witness.nginx.conf').read_text()
        text=w.render(text,self.cfg,8090)
        self.assertIn('auth_request /_immich_witness_route;',text)
        self.assertIn('proxy_next_upstream off;',text)
        self.assertIn('primary 100.64.0.1:8090;',text)
        self.assertIn('backup 100.64.0.2:8090;',text)
        self.assertIn('proxy_request_buffering off;',text)
        self.assertNotIn('$http_x_immich_ha_node',text)
        self.assertIn('User=immich-router',w.UNIT)
        self.assertIn('LoadCredential=client.key:',w.UNIT)

    def test_only_exact_trusted_witness_can_forward_https_scheme(self):
        text=(Path(__file__).parents[1]/'node/immich-ha-local.nginx.conf').read_text()
        rendered=g.render(text,8090,'x'*64,['100.64.0.3'])
        self.assertIn('"100.64.0.3:https" https;',rendered)
        with self.assertRaises(ValueError):g.render(text,8090,'x'*64,['0.0.0.0'])

    def test_deployment_payloads_compile_without_operating_services(self):
        for name in ('PREFLIGHT','TRUST_SCHEME','INSTALL'):compile(getattr(w,name),name,'exec')
        path=Path(__file__).parents[1]/'tools/install-witness-gateway.py'
        spec=importlib.util.spec_from_file_location('witness_install_test',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        compile(module.CHECK,'witness check','exec')
