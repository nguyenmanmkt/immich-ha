import base64
import copy
import json
from pathlib import Path
import sys
import threading
import unittest
import uuid
import importlib.util
sys.path.insert(0,str(Path(__file__).parents[1]/'node'))
from quorum_lock import ShadowLock,validate_config


class FakeEtcd:
    """Atomic KV/lease simulator; no personal config, network or Linux account."""
    def __init__(self):
        self.lock=threading.Lock();self.rows={};self.leases={};self.rev=0;self.clock=0;self.next=1
        self.lose_commit_response=False;self.failed_commit_reply=False

    def call(self,path,payload):
        with self.lock:
            expired={key for key,value in self.leases.items() if value<=self.clock}
            for key in list(self.rows):
                if self.rows[key].get('lease','0') in expired:del self.rows[key]
            if path=='/v3/lease/grant':
                key=str(self.next);self.next+=1;self.leases[key]=self.clock+payload['TTL'];return {'ID':key,'TTL':payload['TTL']}
            if path=='/v3/lease/revoke':
                lease=str(payload['ID']);self.leases.pop(lease,None)
                self.rows={k:v for k,v in self.rows.items() if v.get('lease','0')!=lease};return {}
            assert path=='/v3/kv/txn'
            if not all(int(self.rows.get(c['key'],{}).get('mod_revision',0))==int(c['mod_revision']) for c in payload['compare']):return {'succeeded':False}
            writes=any('request_put' in op for op in payload['success'])
            if writes:self.rev+=1
            responses=[]
            for op in payload['success']:
                if 'request_range' in op:
                    request=op['request_range'];assert request.get('serializable') is False
                    responses.append({'response_range':{'kvs':[copy.deepcopy(self.rows[request['key']])] if request['key'] in self.rows else []}})
                else:
                    request=op['request_put'];self.rows[request['key']]={'value':request['value'],'mod_revision':str(self.rev),'lease':str(request.get('lease','0'))}
                    responses.append({'response_put':{}})
            if writes and self.lose_commit_response:
                self.lose_commit_response=False;raise RuntimeError('simulated lost reply after commit')
            if writes and self.failed_commit_reply:self.failed_commit_reply=False;return {'succeeded':False}
            return {'succeeded':True,'responses':responses}


class ShadowLeaseTests(unittest.TestCase):
    def test_shadow_rollout_embedded_payloads_compile_without_running_installers(self):
        path=Path(__file__).parents[1]/'tools/quorum-shadow.py'
        spec=importlib.util.spec_from_file_location('shadow_rollout_test',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        for name in ('INSTALL','TEST','OBSERVE'):compile(getattr(module,name),name,'exec')

    def setUp(self):
        self.client=FakeEtcd();self.cluster=str(uuid.uuid4());self.shadow=ShadowLock(self.client,self.cluster)

    def claim(self,authority='cloudflare',owner='primary',epoch=14,session='primary_session_1234',ttl=260):
        return self.shadow.claim(authority,owner,epoch,session,ttl)

    def test_renewal_moves_lease_without_deleting_current_lock(self):
        self.assertTrue(self.claim()['admitted']);first=self.shadow.read()[1]['lease']
        self.client.clock=60;self.assertTrue(self.claim()['admitted'])
        current=self.shadow.read()[1];self.assertNotEqual(current['lease'],first)
        self.assertNotIn(first,self.client.leases)
        self.client.clock=280;self.assertIsNotNone(self.shadow.read()[1]['value'])
        self.client.clock=321;generation,active=self.shadow.read()
        self.assertIsNone(active['value']);self.assertEqual(generation['value']['epoch'],14)

    def test_no_higher_epoch_or_fallback_can_preempt_live_cloudflare_lease(self):
        self.claim()
        result=self.claim('fallback','backup',15,'backup_session_12345')
        self.assertFalse(result['admitted']);self.assertEqual(result['reason'],'lease-held')
        self.assertFalse(self.claim('cloudflare','backup',14,'backup_session_12345')['admitted'])
        self.assertEqual(self.shadow.read()[1]['value']['owner'],'primary')

    def test_expired_lock_allows_new_generation_and_persistently_rejects_old_cloudflare(self):
        self.claim();self.client.clock=261
        self.assertTrue(self.claim('fallback','backup',15,'backup_session_12345')['admitted'])
        self.assertFalse(self.claim()['admitted'])
        self.client.clock=600
        self.assertFalse(self.claim()['admitted'])
        self.assertFalse(self.claim('cloudflare','backup',15,'backup_session_12345')['admitted'])
        self.assertTrue(self.claim('cloudflare','backup',16,'backup_session_12345')['admitted'])

    def test_simultaneous_claims_admit_exactly_one_writer(self):
        barrier=threading.Barrier(2);original=self.shadow.read;count=0;guard=threading.Lock()
        def read():
            nonlocal count
            value=original()
            with guard:count+=1;wait=count<=2
            if wait:barrier.wait(timeout=5)
            return value
        self.shadow.read=read;result=[]
        def attempt(authority,owner,epoch,session):result.append(self.claim(authority,owner,epoch,session))
        threads=[threading.Thread(target=attempt,args=('cloudflare','primary',14,'primary_session_1234')),
                 threading.Thread(target=attempt,args=('fallback','backup',15,'backup_session_12345'))]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=8);self.assertFalse(thread.is_alive())
        self.assertEqual(sum(value['admitted'] for value in result),1)

    def test_uncertain_commit_is_not_reported_as_admitted_or_revoked(self):
        self.client.lose_commit_response=True
        with self.assertRaisesRegex(RuntimeError,'lost reply'):self.claim()
        value=self.shadow.read()[1];self.assertIsNotNone(value['value']);self.assertIn(value['lease'],self.client.leases)

    def test_replayed_cas_failure_requires_exact_proposal_confirmation(self):
        self.client.failed_commit_reply=True
        self.assertTrue(self.claim()['admitted'])

    def test_every_write_is_confined_to_shadow_namespace(self):
        self.claim()
        for encoded in self.client.rows:self.assertTrue(base64.b64decode(encoded).decode().startswith('/immich-ha/'+self.cluster+'/shadow/'))
        for scope in ('../active','normal/leases',''):
            with self.assertRaises(ValueError):ShadowLock(self.client,self.cluster,scope)

    def test_configuration_rejects_enforcement_or_untrusted_endpoints(self):
        config={'version':1,'mode':'shadow','clusterId':self.cluster,'node':'primary',
                'endpoints':['https://100.64.0.'+str(i)+':2379' for i in (1,2,3)],
                'ca':'/etc/quorum/ca.crt','cert':'/etc/quorum/client.crt','key':'/etc/quorum/client.key'}
        validate_config(config)
        for mode in ('enforced','http','public','duplicate'):
            value=copy.deepcopy(config)
            if mode=='enforced':value['mode']='enforced'
            elif mode=='http':value['endpoints'][0]='http://100.64.0.1:2379'
            elif mode=='public':value['endpoints'][0]='https://8.8.8.8:2379'
            else:value['endpoints'][0]=value['endpoints'][1]
            with self.assertRaises(ValueError):validate_config(value)


if __name__=='__main__':unittest.main()
