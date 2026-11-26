import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

try:
    import pwd
except ModuleNotFoundError:
    sys.modules["pwd"] = types.SimpleNamespace(getpwnam=lambda _: types.SimpleNamespace(pw_gid=1000))
spec = importlib.util.spec_from_file_location("ha_runtime_tests", Path(__file__).parents[1] / "node/ha_runtime.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        # CI must not depend on an Immich Linux user existing on the runner.
        account = types.SimpleNamespace(pw_uid=999, pw_gid=1000, pw_dir='/opt/immich')
        user = patch.object(module.pwd, 'getpwnam', return_value=account)
        user.start()
        self.addCleanup(user.stop)

    def test_builtin_backup_inventory_excludes_ha_dumps_incomplete_files_and_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            good = root / 'immich-db-backup-20261118T020000-v3.4.1-pg16.16.sql.gz'
            good.write_bytes(b'abc')
            (root / (good.name + '.tmp')).write_bytes(b'incomplete')
            (root / 'immich-20261118-100000.dump').write_bytes(b'ha')
            (root / 'postgres').mkdir()
            result = module.builtin_backups(root)
            self.assertEqual(result['count'], 1)
            self.assertEqual(result['bytes'], 3)
            self.assertEqual(result['files'][0]['name'], good.name)
            self.assertEqual(module.builtin_backups(root, limit=0)['truncated'], True)
            self.assertEqual(module.builtin_backups(root / 'missing')['state'], 'unavailable')

    def test_resources_use_lxc_memory_and_cpu_quota_without_blocking_sampling(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(module, 'ROOT', Path(folder)):
            runtime = self.runtime(folder)
            usage = [1000000]
            def read(path, *args, **kwargs):
                return {'/proc/meminfo': 'MemTotal: 6291456 kB\nMemAvailable: 4194304 kB\n',
                    '/sys/fs/cgroup/cpu.stat': 'usage_usec ' + str(usage[0]),
                    '/sys/fs/cgroup/cpu.max': '200000 100000'}[path.as_posix()]
            with patch.object(module.Path, 'read_text', read), patch.object(module.os, 'sched_getaffinity', return_value=set(range(6)), create=True), patch.object(module.time, 'monotonic', side_effect=[100,102]):
                first = runtime.resources()
                self.assertNotIn('percent', first['cpu'])
                usage[0] = 3000000
                second = runtime.resources()
            self.assertEqual(second['cpu']['cores'], 2)
            self.assertEqual(second['cpu']['percent'], 50)
            self.assertEqual(second['memory']['used'], 2 * 1024**3)
            self.assertEqual(second['memory']['total'], 6 * 1024**3)

    def test_tailnet_peer_and_ping_are_not_inferred_from_unrelated_addresses(self):
        self.assertEqual(module.tailnet_ip('tcp://100.64.0.10:22000'), '100.64.0.10')
        self.assertEqual(module.tailnet_ip('tcp://[fd7a:115c:a1e0::1]:22000'), 'fd7a:115c:a1e0::1')
        self.assertIsNone(module.tailnet_ip('tcp://192.0.2.10:22000'))
        self.assertIsNone(module.tailnet_ip('dynamic'))
        self.assertEqual(module.parse_tail_ping('pong from peer (100.1.2.3) via 192.0.2.10:41641 in 0s'), {'state': 'direct', 'latencyMs': 0.0})
        self.assertEqual(module.parse_tail_ping('pong from peer (100.1.2.3) via DERP(sin) in 30ms'), {'state': 'relay', 'latencyMs': 30.0})
        self.assertEqual(module.parse_tail_ping('pong from peer (100.1.2.3) via 192.0.2.10:41641 in 900µs')['latencyMs'], 0.9)
        self.assertEqual(module.parse_tail_ping('pong from immich (100.64.0.10) via 192.0.2.10:41641 in 2.3ms'),
                         {'state': 'direct', 'latencyMs': 2.3})
        self.assertEqual(module.parse_tail_ping('pong from immich (100.64.0.10) via DERP(sin) in 48ms')['state'], 'relay')
        self.assertEqual(module.parse_tail_ping('ping timed out')['state'], 'unreachable')

    def test_observability_counts_visible_media_on_the_current_database_without_writing(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(module, 'ROOT', Path(folder)):
            runtime = self.runtime(folder)
            def command(*args, **kwargs):
                if args[0] == 'runuser':
                    self.assertIn('--dbname=immich_original', args)
                    self.assertIn('default_transaction_read_only=on', args[-1])
                    self.assertIn("visibility IN ('timeline','archive')", args[-1])
                    return '{"photos":13979,"videos":1596}'
                if args[1] == 'status':
                    return '{"BackendState":"Running","Peer":{"peer":{"Online":true,"TailscaleIPs":["100.64.0.10"]}}}'
                # The real CLI exits nonzero after a successful DERP pong when
                # --until-direct defaults to true. Model that failure here.
                if '--until-direct=false' not in args:
                    raise RuntimeError('direct connection not established')
                return 'pong from immich (100.64.0.10) via DERP(sin) in 40ms'
            with patch.object(module, 'run', side_effect=command):
                runtime.observability('100.64.0.10')
                runtime._observability_thread.join(2)
                result = runtime.observability('100.64.0.10')
            self.assertEqual(result['immich']['photos'], 13979)
            self.assertEqual(result['tailscale']['state'], 'relay')
            self.assertEqual(result['tailscale']['latencyMs'], 40)

    def test_enter_maintenance_stops_app_and_pauses_sync_under_the_runtime_lock(self):
        from contextlib import nullcontext
        with tempfile.TemporaryDirectory() as folder, patch.object(module, "ROOT", Path(folder)):
            runtime = self.runtime(folder)
            with patch.object(runtime, "lock", return_value=nullcontext()) as lock, patch.object(module, "run") as run, patch.object(module, "Syncthing") as sync:
                runtime.enter_maintenance()
            lock.assert_called_once_with(nonblocking=True)
            run.assert_called_once_with("systemctl", "stop", "immich-ha-app.service", "immich-ml.service")
            sync.return_value.pause.assert_called_once()
            self.assertEqual(runtime.mode(), "maintenance")

    def test_busy_runtime_cannot_claim_maintenance_acknowledgement(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(module, "ROOT", Path(folder)):
            runtime = self.runtime(folder)
            with patch.object(runtime, "lock", side_effect=RuntimeError("busy")), patch.object(module, "run") as run:
                with self.assertRaises(RuntimeError): runtime.enter_maintenance()
            run.assert_not_called()
            self.assertEqual(runtime.mode(), "standby")

    def test_maintenance_rejects_new_write_actions_even_if_epoch_matches(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(module, "ROOT", Path(folder)):
            runtime = self.runtime(folder)
            runtime.info = Mock(return_value={"owner":"primary","epoch":1,"nodeMaintenance":{"backup":{"enabled":True}}})
            with patch.object(runtime,"mounted"):
                with self.assertRaisesRegex(RuntimeError,"bảo trì"): runtime.guard({"source":"primary","epoch":1})

    def test_guard_retries_a_timeout_and_still_validates_the_returned_epoch(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(module,"ROOT",Path(folder)):
            runtime=self.runtime(folder)
            runtime.info=Mock(side_effect=[TimeoutError(),{'owner':'primary','epoch':2}])
            with patch.object(runtime,'mounted'),patch.object(module.time,'sleep'):
                with self.assertRaises(RuntimeError):runtime.guard({'source':'primary','epoch':1},source=False)
            self.assertEqual(runtime.info.call_count,2)

    def test_guard_does_not_retry_an_authentication_rejection(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(module,"ROOT",Path(folder)):
            runtime=self.runtime(folder)
            runtime.info=Mock(side_effect=module.urllib.error.HTTPError('https://controller',403,'Forbidden',None,None))
            with patch.object(runtime,'mounted'),patch.object(module.time,'sleep') as sleep:
                with self.assertRaises(module.urllib.error.HTTPError):runtime.guard({'source':'primary','epoch':1},source=False)
            self.assertEqual(runtime.info.call_count,1);sleep.assert_not_called()

    def runtime(self, root):
        runtime = module.Runtime({"node": "backup"}, None, lambda: {"owner": "primary", "epoch": 1}, lambda: True)
        runtime.write = lambda name, value: (Path(root) / name).write_text(value)
        runtime.write("current-db", "immich_original")
        runtime.write("mode", "standby")
        runtime.write("seed.json", '{"sha256":"old"}')
        return runtime

    def test_manifest_rejects_missing_truncated_and_escaping_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "photo.jpg").write_bytes(b"photo")
            self.assertEqual(module.validate_manifest(root, [{"path": "photo.jpg", "size": 5}])["bytes"], 5)
            for entries in ([{"path": "photo.jpg", "size": 6}], [{"path": "missing.jpg", "size": 0}]):
                with self.assertRaises(RuntimeError):
                    module.validate_manifest(root, entries)
            for path in ("../outside", "/absolute", "library/../../outside", "library\\outside", "./photo.jpg"):
                with self.assertRaises(ValueError):
                    module.safe_media_path(root, path)

    def test_restore_failure_keeps_old_database_and_pointer(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(module, "ROOT", Path(folder)):
            runtime = self.runtime(folder)
            dump = Path(folder) / "input.dump"
            dump.write_bytes(b"dump")
            calls = []
            def run(*args, **kwargs):
                calls.append(args)
                return ""
            snapshot = {"source": "primary", "epoch": 1, "sha256": "new", "name": "immich-20261117-120000.dump"}
            with patch.object(runtime, "guard", return_value={"owner": "primary", "epoch": 1}), \
                 patch.object(runtime, "preflight_space"), patch.object(runtime, "repair_media_access"), patch.object(runtime, "read_snapshot", return_value=dump), \
                 patch.object(runtime, "wait_healthy", side_effect=RuntimeError("candidate unhealthy")), \
                 patch.object(module, "run", side_effect=run):
                with self.assertRaisesRegex(RuntimeError, "candidate unhealthy"):
                    runtime.restore(snapshot, {"source": "primary", "epoch": 1}, lambda _: None)
            self.assertEqual(runtime.database(), "immich_original")
            self.assertEqual(runtime.mode(), "standby")
            self.assertEqual(runtime.seed(), {"sha256": "old"})
            drops = [args for args in calls if "dropdb" in args]
            self.assertEqual(len(drops), 1)
            self.assertTrue(drops[0][-1].startswith("immich_ha_"))
            self.assertNotIn("immich_original", drops[0])

    def test_stale_source_epoch_is_rejected_before_mutation(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(module, "ROOT", Path(folder)):
            runtime = self.runtime(folder)
            with patch.object(runtime, "mounted"), patch.object(module, "run") as run:
                with self.assertRaises(RuntimeError):
                    runtime.guard({"source": "primary", "epoch": 0}, source=False)
                run.assert_not_called()

    def test_frozen_source_cannot_restart_from_a_heartbeat(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(module, "ROOT", Path(folder)):
            runtime = self.runtime(folder)
            runtime.write("mode", "frozen")
            with patch.object(runtime, "mounted"), patch.object(module, "run") as run:
                self.assertFalse(runtime.activate())
                run.assert_not_called()

    def test_healthy_active_lease_renewal_does_not_wait_for_backup_lock(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(module, "ROOT", Path(folder)):
            runtime = self.runtime(folder)
            runtime.write("mode", "active")
            runtime._activated = True
            with patch.object(runtime, "mounted"), patch.object(runtime, "lock", side_effect=RuntimeError("backup lock busy")) as lock, patch.object(module, "run") as run:
                self.assertTrue(runtime.activate())
                lock.assert_not_called()
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
