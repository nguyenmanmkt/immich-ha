import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

sys.path.insert(0, str(Path(__file__).parents[1] / 'node'))
from quorum_authority import Admission, ControlLock, assert_writer, validate_enforced_config
import quorum_authority as qa
from test_quorum_lock import FakeEtcd


class DeletingEtcd(FakeEtcd):
    def call(self, path, payload):
        # Extend the transactional simulator with exact-key deletion.
        if path == '/v3/kv/txn' and any('request_delete_range' in x for x in payload['success']):
            with self.lock:
                if not all(int(self.rows.get(c['key'], {}).get('mod_revision', 0)) == int(c['mod_revision']) for c in payload['compare']):
                    return {'succeeded': False}
                self.rev += 1
                for op in payload['success']:
                    if 'request_delete_range' in op:self.rows.pop(op['request_delete_range']['key'], None)
                    else:
                        r = op['request_put']
                        self.rows[r['key']] = dict(value=r['value'], mod_revision=str(self.rev), lease=str(r.get('lease', '0')))
                return {'succeeded': True}
        return super().call(path, payload)


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.client = DeletingEtcd()
        self.cluster = str(uuid.uuid4())
        self.lock = ControlLock(self.client, self.cluster)
        self.session = 'persistent_session_12345'

    def claim(self, owner='primary', epoch=14, session=None, authority='cloudflare', **extra):
        return self.lock.claim(authority, owner, epoch, session or self.session, 260,
            policy=dict(sequence=1,auto=True,blocked=False,excluded={'primary':False,'backup':False}),**extra)

    def config(self):
        return dict(version=1, mode='enforced', fallbackEnabled=False, clusterId=self.cluster, node='primary',
                    endpoints=['https://100.64.0.' + str(i) + ':2379' for i in (1, 2, 3)],
                    ca='/etc/quorum/ca', cert='/etc/quorum/cert', key='/etc/quorum/key')

    def test_live_cloudflare_lock_blocks_other_cloudflare_and_fallback(self):
        self.assertTrue(self.claim()['admitted'])
        for authority in ('cloudflare', 'fallback'):
            self.assertFalse(self.claim('backup', 15, 'other_session_123456', authority)['admitted'])
        self.assertIn('/control/admission/', self.lock.keys[0])

    def test_native_expiry_and_persistent_generation_reject_old_writer(self):
        self.claim()
        self.client.clock = 261
        self.assertTrue(self.claim('backup', 15, 'other_session_123456', 'fallback')['admitted'])
        self.assertFalse(self.claim()['admitted'])
        self.client.clock = 600
        self.assertFalse(self.claim()['admitted'])

    def test_durable_release_allows_fast_next_epoch_but_never_routine_reopen(self):
        self.claim()
        self.lock.release('primary', 14, self.session)
        self.assertIsNone(self.lock.read()[1]['value'])
        self.assertEqual(self.claim()['reason'], 'durably-released')
        self.assertTrue(self.claim('backup', 15, 'other_session_123456')['admitted'])
        self.assertFalse(self.claim(resume=True)['admitted'])

    def test_explicit_resume_only_reopens_exact_original_generation(self):
        self.claim()
        self.lock.release('primary', 14, self.session)
        self.assertFalse(self.claim(session='different_session_1234', resume=True)['admitted'])
        self.assertFalse(self.claim(authority='fallback', resume=True)['admitted'])
        self.assertTrue(self.claim(resume=True)['admitted'])

    def test_cannot_release_another_owner_or_newer_epoch(self):
        self.claim()
        for owner, epoch in [('backup', 14), ('primary', 13)]:
            with self.assertRaises(RuntimeError):self.lock.release(owner, epoch, self.session)
        self.assertIsNotNone(self.lock.read()[1]['value'])

    def test_uncertain_commit_cannot_start_writer(self):
        self.client.lose_commit_response = True
        with self.assertRaises(RuntimeError):self.claim()
        self.assertIsNotNone(self.lock.read()[1]['value'])

    def test_fallback_requires_explicit_boolean_configuration(self):
        cfg = self.config()
        validate_enforced_config(cfg)
        self.assertFalse(cfg['fallbackEnabled'])
        cfg['fallbackEnabled'] = 'true'
        with self.assertRaises(ValueError):validate_enforced_config(cfg)

    def admission(self, root, clock=lambda: 100):
        return Admission(self.config(), self.session, 1000, client=self.client, clock=clock,
                         grant_path=Path(root) / 'grant.json')

    def test_host_suspend_expires_quorum_permission_and_startup_proof(self):
        now = [100]
        with tempfile.TemporaryDirectory() as root, patch.object(qa.os, 'chown', create=True), patch.object(qa.BOOT_ID.__class__, 'read_text', return_value='test-boot'):
            a = self.admission(root, lambda: now[0])
            with patch.object(qa.time, 'monotonic', return_value=100):
                a.admit(dict(epoch=14, deadline=330))
            self.assertTrue(a.valid(14))
            proof = json.loads(a.path.read_bytes())
            self.assertEqual(proof['bootId'], 'test-boot')
            self.assertLessEqual(proof['deadline'], 330)
            now[0] = 331
            self.assertFalse(a.valid(14))
            with self.assertRaises(RuntimeError):a.assert_grant(14, fresh=True)

    def test_writer_and_jobs_reject_new_quorum_generation(self):
        with tempfile.TemporaryDirectory() as root, patch.object(qa.os, 'chown', create=True), patch.object(qa.BOOT_ID.__class__, 'read_text', return_value='boot'):
            a = self.admission(root)
            with patch.object(qa.time, 'monotonic', return_value=100):a.admit(dict(epoch=14, deadline=330))
            a.guard_command(dict(source='primary', epoch=14))
            self.client.clock = 300
            self.claim('backup', 15, 'other_session_123456')
            with self.assertRaises(RuntimeError):a.guard_command(dict(source='primary', epoch=14))
            with self.assertRaises(RuntimeError):a.assert_grant(14, fresh=True)
            self.assertFalse(a.path.exists())

    def test_required_marker_without_loaded_authority_blocks_writer(self):
        runtime = Mock()
        with patch.object(qa.REQUIRED.__class__, 'exists', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'not initialized'):assert_writer(runtime)

    def test_streaming_promote_and_upgrade_start_cannot_bypass_admission(self):
        import pg_stream
        import immich_upgrade
        runtime = Mock()
        runtime.grant = dict(epoch=14)
        runtime.quorum_authority = Mock(spec=Admission)
        runtime.quorum_authority.assert_grant.side_effect = RuntimeError('quorum expired')
        stream = pg_stream.Streaming(runtime, {})
        upgrade = object.__new__(immich_upgrade.Upgrader)
        upgrade.r = runtime
        with patch.object(pg_stream, 'pg') as promote, patch.object(pg_stream, 'ctl') as ctl:
            with self.assertRaisesRegex(RuntimeError, 'quorum expired'):stream.activate()
            with self.assertRaisesRegex(RuntimeError, 'quorum expired'):upgrade.start()
            promote.assert_not_called();ctl.assert_not_called()

    def test_rollout_payloads_compile_without_network_or_installer(self):
        path = Path(__file__).parents[1] / 'tools/quorum-enforce.py'
        if not path.exists():self.skipTest('Rollout tool not yet present')
        spec = importlib.util.spec_from_file_location('quorum_enforce_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name in ('PREFLIGHT', 'INSTALL', 'ENABLE', 'CHECK'):compile(getattr(module, name), name, 'exec')
