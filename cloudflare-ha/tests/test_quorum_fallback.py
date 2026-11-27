import json
import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock,patch
import uuid

try:
    import pwd
except ModuleNotFoundError:
    sys.modules['pwd']=types.SimpleNamespace(getpwnam=lambda _:types.SimpleNamespace(pw_uid=999,pw_gid=1000,pw_dir='/opt/immich'))

sys.path.insert(0,str(Path(__file__).parents[1]/'node'))
import quorum_authority as q
from test_quorum_authority import DeletingEtcd


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.client=DeletingEtcd();self.cluster=str(uuid.uuid4());self.now=100
        self.policy=dict(sequence=1,auto=True,blocked=False,excluded={'primary':False,'backup':False})
        patches=[patch.object(q.os,'chown',create=True),patch.object(q.BOOT_ID.__class__,'read_text',return_value='boot'),
                 patch.object(q.time,'monotonic',side_effect=lambda:self.now)]
        for p in patches:p.start();self.addCleanup(p.stop)

    def node(self,node):
        config=dict(version=1,mode='enforced',fallbackEnabled=True,clusterId=self.cluster,node=node,
            endpoints=['https://100.64.0.'+str(i)+':2379' for i in (1,2,3)],ca='/etc/q/ca',cert='/etc/q/cert',key='/etc/q/key')
        a=q.Admission(config,node+'_session_12345678',1000,client=self.client,clock=lambda:self.now,
                      grant_path=Path(self.directory.name)/(node+'.json'))
        r=Mock();r.mode.return_value='active' if node=='primary' else 'standby';r.read.return_value=''
        r.streaming.state.return_value={'inRecovery':node!='primary'};r.healthy.return_value=True;r.can_promote.return_value=True
        agent=types.SimpleNamespace(node=node,session=a.session,runtime=r,quorum_authority=a,
            grant_mutex=threading.RLock(),view=dict(role='standby',epoch=14,deadline=0,ready=False),
            activate=Mock(),fence=Mock(),grant_valid=lambda:True,enforcing=lambda:True,next_poll_seconds=60)
        fallback=q.Fallback(agent)
        return a,agent,fallback

    def advance(self,seconds):self.now+=seconds;self.client.clock+=seconds

    def source(self):
        a,agent,f=self.node('primary')
        a.admit(dict(epoch=14,deadline=self.now+230),policy=self.policy)
        return a,agent,f

    def response(self,**extra):
        return dict(role='quorum-wait',owner='primary',epoch=14,leaseMs=0,quorum=dict(protocol=2,
            clusterId=self.cluster,mode='fallback',sequence=2,auto=True,blocked=False,
            excluded=self.policy['excluded'],**extra))

    def test_outage_continues_same_writer_only_after_grace_and_never_creates_second_writer(self):
        a,agent,f=self.source()
        f.failure();self.assertEqual(a.lock.read()[1]['value']['authority'],'cloudflare')
        self.advance(16);f.maintain()
        record=a.lock.read()[1]['value']
        self.assertEqual(record['authority'],'fallback');self.assertEqual(record['epoch'],14)
        self.assertEqual(agent.view['role'],'active');agent.activate.assert_called_once()
        denied=a.lock.claim('cloudflare','backup',15,'backup_session_12345678',100,policy=self.policy)
        self.assertFalse(denied['admitted'])
        f.maintain();self.assertEqual(a.lock.read()[1]['value']['fallbackTerm'],record['fallbackTerm'])

    def test_policy_updates_are_monotonic_and_operations_prevent_fallback(self):
        a,agent,f=self.source()
        blocked={**self.policy,'sequence':3,'blocked':True}
        a.lock.update_policy('primary',14,blocked)
        a.lock.update_policy('primary',14,{**self.policy,'sequence':2})
        f.failure();self.advance(20);f.maintain()
        self.assertEqual(a.lock.read()[1]['value']['authority'],'cloudflare')
        agent.activate.assert_not_called()

    def test_durable_local_freeze_blocks_fallback_before_mode_or_services_change(self):
        a,agent,f=self.source();a.local_block(True)
        f.failure();self.advance(20);f.maintain()
        self.assertEqual(a.lock.read()[1]['value']['authority'],'cloudflare')
        self.assertTrue(a.lock.read()[0]['value']['localBlocked'])
        agent.activate.assert_not_called()

    def test_same_writer_handback_requires_nonce_and_preserves_owner_epoch(self):
        a,agent,f=self.source();f.failure();self.advance(20);f.maintain()
        report=f.report();self.assertTrue(report['localGrant'])
        challenge=dict(owner='primary',session=a.session,targetEpoch=14,nonce='b'*32)
        self.assertFalse(f.response(self.response(challenge=challenge)))
        self.assertEqual(a.lock.read()[1]['value']['authority'],'handoff')
        with self.assertRaises(RuntimeError):a.admit(dict(epoch=14,deadline=self.now+230),policy=self.policy)
        # The same exact handoff nonce permits the authority change.
        a.admit(dict(epoch=14,deadline=self.now+230),policy=self.policy,ack=challenge['nonce'])
        self.assertEqual(a.lock.read()[1]['value']['authority'],'cloudflare')
        self.assertEqual(f.report()['cfAck'],challenge['nonce'])
        first_term=f.report()['term']
        f.failure();self.advance(20);f.maintain()
        self.assertNotEqual(f.report()['term'],first_term)
        self.assertIsNone(f.report()['cfAck'])

    def test_cf_handoff_failure_can_renew_same_writer_without_admitting_old_cf(self):
        a,agent,f=self.source();f.failure();self.advance(20);f.maintain()
        challenge=dict(owner='primary',session=a.session,targetEpoch=18,nonce='c'*32)
        f.response(self.response(challenge=challenge))
        self.advance(10);f.failure();f.maintain()
        current=a.lock.read()[1]['value']
        self.assertEqual(current['authority'],'handoff');self.assertEqual(current['epoch'],18)
        self.assertFalse(a.lock.claim('cloudflare','primary',14,a.session,100,policy=self.policy)['admitted'])

    def test_standby_waits_for_native_expiry_confirmation_wal_and_media_before_promotion(self):
        source,_,_=self.source();a,agent,f=self.node('backup')
        self.advance(300);f.maintain();agent.activate.assert_not_called()
        self.advance(299);f.maintain();agent.activate.assert_not_called()
        sync=Mock();sync.folder.return_value={'type':'receiveonly'}
        sync.status.return_value={'state':'idle','needTotalItems':0,'needBytes':0}
        self.advance(1)
        with patch('ha_runtime.Syncthing',return_value=sync):f.maintain()
        self.assertEqual(a.lock.read()[1]['value']['owner'],'backup')
        self.assertEqual(agent.view['epoch'],15);agent.activate.assert_called_once()

    def test_missing_wal_media_or_disabled_auto_never_promotes(self):
        source,_,_=self.source();a,agent,f=self.node('backup')
        self.advance(300);f.maintain();self.advance(301)
        agent.runtime.can_promote.return_value=False
        f.maintain();agent.activate.assert_not_called()
        agent.runtime.can_promote.return_value=True
        sync=Mock();sync.folder.return_value={'type':'receiveonly'}
        sync.status.return_value={'state':'idle','needTotalItems':1,'needBytes':20}
        with patch('ha_runtime.Syncthing',return_value=sync):f.maintain()
        agent.activate.assert_not_called()
        a.lock.update_policy('primary',14,{**self.policy,'sequence':4,'auto':False})
        sync.status.return_value={'state':'idle','needTotalItems':0,'needBytes':0}
        with patch('ha_runtime.Syncthing',return_value=sync):f.maintain()
        agent.activate.assert_not_called()

    def test_stale_network_jobs_are_rejected_in_fallback_and_handoff(self):
        a,agent,f=self.source();f.failure();self.advance(20);f.maintain()
        with self.assertRaises(RuntimeError):a.guard_command(dict(source='primary',epoch=14))
        self.assertEqual(f.info()['owner'],'primary')

    def test_quorum_failure_cannot_manufacture_a_fallback_grant(self):
        a,agent,f=self.source();f.failure();self.advance(20)
        with patch.object(self.client,'call',side_effect=RuntimeError('no majority')):
            with self.assertRaises(RuntimeError):f.maintain()
        agent.activate.assert_not_called()

    def test_competing_cf_and_fallback_claims_admit_only_one_writer(self):
        a,agent,f=self.node('primary');barrier=threading.Barrier(2)
        original=a.lock.read;count=[0];guard=threading.Lock()
        def read():
            value=original()
            with guard:count[0]+=1;wait=count[0]<=2
            if wait:barrier.wait(timeout=5)
            return value
        a.lock.read=read;results=[]
        def attempt(authority,owner,epoch):
            results.append(a.lock.claim(authority,owner,epoch,owner+'_session_12345678',180,policy=self.policy))
        workers=[threading.Thread(target=attempt,args=('cloudflare','primary',14)),threading.Thread(target=attempt,args=('fallback','backup',15))]
        for w in workers:w.start()
        for w in workers:w.join(timeout=8);self.assertFalse(w.is_alive())
        self.assertEqual(sum(r['admitted'] for r in results),1)

    def test_cancelled_cloudflare_job_cannot_resume_after_handback_even_at_same_epoch(self):
        from ha_runtime import Runtime
        r=object.__new__(Runtime);r.mounted=Mock();r.node='primary';r.mode=Mock(return_value='active')
        r.quorum_authority=Mock(spec=q.Admission)
        r.info=Mock(return_value=dict(owner='primary',epoch=14,operation=dict(id='old-job',status='failed')))
        with self.assertRaisesRegex(RuntimeError,'cũ không còn'):r.guard(dict(source='primary',epoch=14,jobId='old-job'),source=True)

    def test_fallback_rollout_payloads_compile_without_installing(self):
        path=Path(__file__).parents[1]/'tools/quorum-fallback.py'
        spec=importlib.util.spec_from_file_location('fallback_rollout_test',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        for name in ('ENABLE','CHECK'):compile(getattr(module,name),name,'exec')

    def test_live_heartbeat_test_payloads_compile_without_running_services(self):
        path=Path(__file__).parents[1]/'tools/quorum-test-cloudflare.py'
        spec=importlib.util.spec_from_file_location('heartbeat_fault_test',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        for name in ('SAMPLE','INSTALL','ARM','RESTORE','OBSERVE'):compile(getattr(module,name),name,'exec')


if __name__=='__main__':unittest.main()
