import contextlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

try:
    import pwd
except ModuleNotFoundError:
    sys.modules['pwd']=types.SimpleNamespace(getpwnam=lambda _:types.SimpleNamespace(pw_uid=999,pw_gid=1000))

sys.path.insert(0, str(Path(__file__).parents[1] / 'node'))
import pg_stream as pg


class StreamingTests(unittest.TestCase):
    def test_start_waits_for_local_tailnet_address_before_launching_postgres(self):
        with patch.object(pg, 'wait_local_address') as wait, patch.object(pg.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            pg.ctl('start')
            wait.assert_called_once_with()
            run.assert_called_once()
        with patch.object(pg, 'wait_local_address', side_effect=RuntimeError('not ready')), patch.object(pg.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'not ready'):
                pg.ctl('start')
            run.assert_not_called()

    def test_missing_tailnet_address_does_not_prevent_stop(self):
        with patch.object(pg, 'wait_local_address') as wait, patch.object(pg.subprocess, 'run', return_value=Mock(returncode=0)):
            pg.ctl('stop')
            wait.assert_not_called()

    def test_network_wait_requires_configured_ip_on_tailnet_interface(self):
        replies = [Mock(returncode=0, stdout='[{"addr_info":[{"local":"100.64.0.99"}]}]'),
                   Mock(returncode=0, stdout='[{"addr_info":[{"local":"100.64.0.2"}]}]')]
        with patch.object(pg, 'configuration', return_value={'local_ip':'100.64.0.2'}), \
                patch.object(pg.subprocess, 'run', side_effect=replies), patch.object(pg.time, 'sleep') as sleep:
            pg.wait_local_address()
            sleep.assert_called_once_with(1)

    def test_network_wait_fails_closed_when_address_never_arrives(self):
        with patch.object(pg, 'configuration', return_value={'local_ip':'100.64.0.2'}), \
                patch.object(pg.subprocess, 'run', return_value=Mock(returncode=0, stdout='[]')):
            with self.assertRaisesRegex(RuntimeError, 'Tailscale'):
                pg.wait_local_address(timeout=0)

    def test_network_dropin_is_idempotent_and_does_not_start_database(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(pg, 'NETWORK_DROP_IN', Path(tmp)/'unit.d/guard.conf'), \
                patch.object(pg, 'configuration', return_value={'local_ip':'100.64.0.2'}), patch.object(pg.subprocess, 'run') as run:
            self.assertTrue(pg.install_network_guard())
            self.assertIn('After=tailscaled.service', pg.NETWORK_DROP_IN.read_text())
            self.assertIn('wait_local_address()', pg.NETWORK_DROP_IN.read_text())
            self.assertFalse(pg.install_network_guard())
            run.assert_called_once_with(['systemctl','daemon-reload'], check=True, capture_output=True, timeout=30)

    def runtime(self):
        r = Mock()
        r.mode.return_value = 'standby'
        r.lock.return_value = contextlib.nullcontext()
        r.seed.return_value = {'source':'primary','sourceEpoch':7,'systemId':'123'}
        r.read.side_effect = lambda name, default='': '1000' if name == 'stream-last-connected' else default
        r.info.return_value = {'owner':'backup','epoch':8}
        r.node = 'backup'
        return r

    def stream(self, r=None):
        return pg.Streaming(r or self.runtime(), {'peer':'primary','peer_ip':'100.64.0.2','database':'immich_stream','port':5433})

    def state(self, **extra):
        return {'inRecovery':True,'pendingBytes':0,'receiverConnected':False,'systemId':'123', **extra}

    def test_disabled_configuration_does_not_change_database_mode(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(pg,'CONFIG',Path(tmp)/'stream.json'):
            self.assertIsNone(pg.configuration())
            pg.CONFIG.write_text(json.dumps({'enabled':False}))
            self.assertIsNone(pg.configuration())

    def test_configuration_rejects_public_peer_and_arbitrary_ports(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(pg,'CONFIG',Path(tmp)/'stream.json'):
            for ip, port in [('8.8.8.8',5433),('100.64.0.2',5432)]:
                pg.CONFIG.write_text(json.dumps({'enabled':True,'peer_ip':ip,'peer':'primary','port':port,'database':'immich_stream'}))
                with self.assertRaises(ValueError): pg.configuration()

    def test_promotion_eligibility_rejects_old_epoch_divergence_lag_and_stale_receiver(self):
        s = self.stream()
        with patch.object(s,'state',return_value=self.state()), patch.object(pg.time,'time',return_value=1100):
            self.assertTrue(s.can_promote(7,'primary'))
            self.assertFalse(s.can_promote(8,'primary'))
            self.assertFalse(s.can_promote(7,'backup'))
        for state in [self.state(systemId='999'),self.state(inRecovery=False),self.state(pendingBytes=1)]:
            with patch.object(s,'state',return_value=state):
                self.assertFalse(s.can_promote(7,'primary'))
        with patch.object(s,'state',return_value=self.state()), patch.object(pg.time,'time',return_value=2000):
            self.assertFalse(s.can_promote(7,'primary'))

    def test_activation_without_current_ownership_never_promotes(self):
        r = self.runtime();r.info.return_value={'owner':'primary','epoch':7}
        with patch.object(pg,'pg') as sql, patch.object(pg,'ctl') as ctl:
            with self.assertRaises(RuntimeError): self.stream(r).activate()
            sql.assert_not_called();ctl.assert_not_called()

    def test_healthy_active_renews_readiness_without_contending_with_preparation_job(self):
        r=self.runtime();r._activated=True;r.mode.return_value='active';r.healthy.return_value=True
        r.grant={'role':'active','owner':'backup','epoch':8,'deadline':1000}
        with patch.object(pg.time,'monotonic',return_value=100),patch.object(pg,'pg') as sql:
            self.assertTrue(self.stream(r).activate())
            r.lock.assert_not_called();r.write.assert_not_called();r.info.assert_not_called();sql.assert_not_called()
            r.mode.return_value='frozen'
            self.assertFalse(self.stream(r).activate())

    def test_expired_or_wrong_epoch_lease_never_promotes(self):
        for grant in [{'role':'active','epoch':7,'deadline':10000},
                      {'role':'active','epoch':8,'deadline':0},
                      {'role':'standby','epoch':8,'deadline':10000}]:
            r=self.runtime();r.grant=grant
            with patch.object(pg.time,'monotonic',return_value=100), patch.object(pg,'pg') as sql:
                with self.assertRaises(RuntimeError):self.stream(r).activate()
                sql.assert_not_called()

    def test_standby_health_uses_read_only_database_without_running_immich(self):
        s=self.stream();s.runtime.healthy.return_value=False
        with patch.object(s,'state',return_value=self.state()):
            self.assertTrue(s.healthy())
            s.runtime.healthy.assert_not_called()

    def test_replay_barrier_requires_same_physical_cluster(self):
        s = self.stream()
        with patch.object(s,'state',return_value=self.state(systemId='999')), patch.object(pg,'pg') as sql:
            with self.assertRaises(RuntimeError):
                s.wait_replay({'walLsn':'0/ABC','systemId':'123'},{},lambda _:None)
            sql.assert_not_called()

    def test_invalid_wal_barrier_is_never_interpolated_into_sql(self):
        with patch.object(pg,'pg') as sql:
            with self.assertRaises(RuntimeError):
                self.stream().wait_replay({'walLsn':"0/1'; SELECT 1;",'systemId':'123'},{},lambda _:None)
            sql.assert_not_called()

    def test_clone_does_not_stop_or_replace_existing_database_when_copy_fails(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(pg,'DATA',Path(tmp)/'ha'), \
                patch.object(pg.shutil,'disk_usage',return_value=types.SimpleNamespace(free=3*1024**3)), \
                patch.object(__import__('pwd'),'getpwnam',return_value=types.SimpleNamespace(pw_uid=1,pw_gid=1)), \
                patch.object(pg.os,'chown',create=True), patch.object(pg.subprocess,'run',return_value=types.SimpleNamespace(returncode=1)), \
                patch.object(pg,'ctl') as ctl:
            pg.DATA.mkdir();(pg.DATA/'keep').write_text('old')
            with self.assertRaises(RuntimeError): pg.clone(self.stream().config)
            self.assertEqual((pg.DATA/'keep').read_text(),'old');ctl.assert_not_called()

    def test_dump_refresh_is_disabled_in_streaming_mode(self):
        import ha_runtime
        r = object.__new__(ha_runtime.Runtime)
        r.streaming = Mock();r.mounted=Mock();r.info=Mock()
        r.maintenance('refresh')
        r.info.assert_not_called()

    def test_scheduled_dump_policy_skips_before_any_database_or_network_access(self):
        import ha_runtime
        r=object.__new__(ha_runtime.Runtime)
        r.mounted=Mock();r.info=Mock();r.snapshot=Mock()
        with tempfile.TemporaryDirectory() as tmp, patch.object(ha_runtime,'SCHEDULED_DUMP_DISABLED',Path(tmp)/'disabled'):
            ha_runtime.SCHEDULED_DUMP_DISABLED.touch()
            r.maintenance('dump')
        r.mounted.assert_not_called();r.info.assert_not_called();r.snapshot.assert_not_called()

    @contextlib.contextmanager
    def barrier_fixture(self):
        import ha_runtime
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);(base/'state').mkdir()
            r=self.runtime();r.node='primary';r.group_owner=0;r.group=0
            r.manifest.return_value=[{'path':'library/file.jpg','size':3}]
            r.snapshot.side_effect=AssertionError('WAL handover must never create a dump')
            s=self.stream(r)
            command={'id':'12345678-1234-1234-1234-123456789abc-3',
                'jobId':'12345678-1234-1234-1234-123456789abc','source':'primary','epoch':7}
            with patch.object(ha_runtime,'ROOT',base/'state'),patch.object(ha_runtime,'MEDIA',base/'media'), \
                    patch.object(pg,'GRANT',base/'grant'),patch.object(pg.os,'chown',create=True), \
                    patch.object(ha_runtime,'run'),patch.object(s,'state',return_value=self.state(inRecovery=False)), \
                    patch.object(s,'frozen_proof',return_value={'fenceProtocol':1,'appStopped':True,'postgresStopped':True,'originFenced':True,'frozenDurable':True}), \
                    patch.object(pg,'pg',return_value=json.dumps({'lsn':'0/ABC','walFile':'000000050000000000000001'})) as sql:
                yield s,r,command,base,sql

    def test_barrier_writes_only_manifest_and_retries_without_reading_database(self):
        with self.barrier_fixture() as (s,r,c,base,sql):
            ticket=s.barrier(c,lambda _:None)
            self.assertEqual(ticket['timeline'],5)
            self.assertEqual(ticket['walLsn'],'0/ABC')
            self.assertEqual(list((base/'media').rglob('*.dump')),[])
            sql.reset_mock()
            self.assertEqual(s.barrier(c,lambda _:None),ticket)
            sql.assert_not_called();r.snapshot.assert_not_called()

    def test_freeze_caches_peer_ack_and_retries_after_source_database_stopped(self):
        with self.barrier_fixture() as (s,r,c,base,sql),patch.object(pg,'ctl') as ctl:
            sql.side_effect=[json.dumps({'lsn':'0/ABC','walFile':'000000050000000000000001'}),'t']
            ticket=s.barrier(c,lambda _:None,frozen=True)
            self.assertTrue(ticket['stopped']);self.assertTrue(ticket['peerAcknowledged'])
            self.assertIn("client_addr='100.64.0.2'",sql.call_args.args[0])
            ctl.assert_called_once_with('stop')
            sql.reset_mock();s.barrier(c,lambda _:None,frozen=True)
            sql.assert_not_called();r.snapshot.assert_not_called()

    def test_freeze_timeout_does_not_stop_database_or_claim_final_barrier(self):
        with self.barrier_fixture() as (s,r,c,base,sql),patch.object(pg,'ctl') as ctl, \
                patch.object(pg.time,'monotonic',side_effect=[0,181]):
            sql.side_effect=[json.dumps({'lsn':'0/ABC','walFile':'000000050000000000000001'}),'f']
            with self.assertRaisesRegex(RuntimeError,'WAL cuối'):
                s.barrier(c,lambda _:None,frozen=True)
            ctl.assert_not_called()
            ticket=json.loads((base/'state/wal-barriers'/(c['id']+'.json')).read_text())
            self.assertFalse(ticket['stopped']);self.assertFalse(ticket['peerAcknowledged'])

    def test_sync_wal_never_restores_dump_and_requires_every_manifest_file(self):
        import ha_runtime
        with self.barrier_fixture() as (source,r,c,base,sql):
            ticket=source.barrier(c,lambda _:None)
            target=self.stream();target.runtime.node='backup'
            command={**c,'id':c['jobId']+'-4','snapshot':ticket}
            state=self.state(receiverConnected=True,sender='100.64.0.2',receiverTimeline=5,replayLsn='0/ABC')
            with patch.object(target,'state',return_value=state),patch.object(pg,'clone') as clone:
                sql.side_effect=None;sql.return_value='t'
                with self.assertRaisesRegex(RuntimeError,'Ảnh/video chưa đủ'):
                    target.sync_wal(command,lambda _:None,Mock(),lambda:None)
                target.runtime.write.assert_not_called()
                path=base/'media/library/file.jpg';path.parent.mkdir();path.write_bytes(b'abc')
                result=target.sync_wal(command,lambda _:None,Mock(),lambda:None)
                self.assertTrue(result['manifestVerified']);self.assertEqual(result['filesVerified'],1)
                clone.assert_not_called();target.runtime.snapshot.assert_not_called()

    def test_final_sync_cannot_reseed_from_a_stopped_source_or_wrong_timeline(self):
        with self.barrier_fixture() as (source,r,c,base,sql):
            ticket=source.barrier(c,lambda _:None);ticket.update(stopped=True,peerAcknowledged=True)
            target=self.stream();target.runtime.seed.return_value={'timeline':4}
            with patch.object(target,'state',return_value=self.state(receiverConnected=False)),patch.object(pg,'clone') as clone:
                with self.assertRaises(RuntimeError):target.sync_wal({**c,'snapshot':ticket,'final':True},lambda _:None,Mock(),lambda:None)
                clone.assert_not_called()

    def test_fast_fence_requires_stopped_services_and_fsync_of_mode_and_directory(self):
        import ha_runtime
        for active,postgres_code,valid in [('inactive\ninactive\n',3,True),('active\ninactive',3,False),('inactive\ninactive',0,False)]:
            with tempfile.TemporaryDirectory() as tmp,patch.object(ha_runtime,'ROOT',Path(tmp)), \
                    patch.object(pg,'GRANT',Path(tmp)/'grant'),patch.object(pg.os,'fsync') as fsync, \
                    patch.object(pg.os,'O_DIRECTORY',0,create=True),patch.object(pg.os,'open',return_value=999),patch.object(pg.os,'close'), \
                    patch.object(pg.subprocess,'run',side_effect=[types.SimpleNamespace(returncode=0,stdout=active),types.SimpleNamespace(returncode=postgres_code)]):
                (Path(tmp)/'mode').write_text('frozen\n')
                s=self.stream();s.runtime.mode.return_value='frozen'
                s.runtime.grant={'epoch':7}
                s.runtime.quorum_authority=Mock()
                def release(epoch):
                    self.assertEqual(epoch,7)
                    self.assertEqual(fsync.call_count,2)
                s.runtime.quorum_authority.release_frozen.side_effect=release
                if valid:
                    self.assertTrue(s.frozen_proof()['frozenDurable']);self.assertEqual(fsync.call_count,2)
                    s.runtime.quorum_authority.release_frozen.assert_called_once_with(7)
                else:
                    with self.assertRaises(RuntimeError):s.frozen_proof()
                    fsync.assert_not_called()
                    s.runtime.quorum_authority.release_frozen.assert_not_called()


if __name__ == '__main__': unittest.main()
