import importlib.util
import tempfile
import unittest
import sys
import types
from pathlib import Path
from unittest.mock import patch

try:
    import pwd
except ModuleNotFoundError:  # Unit tests run on Windows; production runs on Debian.
    sys.modules["pwd"] = types.SimpleNamespace(
        getpwnam=lambda _: types.SimpleNamespace(pw_gid=1000, pw_dir="/opt/immich"))

spec = importlib.util.spec_from_file_location("ha_agent", Path(__file__).parents[1] / "node/agent.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AgentTests(unittest.TestCase):
    def test_local_gateway_requires_live_active_enforced_quorum_without_client_epoch(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as root:
            agent=self.make_agent(root)
            agent.quorum_authority=Mock();agent.quorum_authority.valid.return_value=True
            agent.runtime=Mock();agent.runtime.mode.return_value='active'
            agent.view=dict(role='active',ready=True,epoch=14,deadline=1000)
            handler=object.__new__(module.handler_for(agent));handler.path='/authorize-local'
            handler.headers={'X-Immich-HA-Token':agent.config['origin_token']}
            handler.send_response=Mock();handler.send_header=Mock();handler.end_headers=Mock()
            with patch.object(module.time,'monotonic',return_value=100),patch.object(module.os.path,'ismount',return_value=True):
                handler.do_GET();handler.send_response.assert_called_with(200)
                agent.view['role']='standby';handler.do_GET();handler.send_response.assert_called_with(403)
                agent.view['role']='active';agent.quorum_authority.valid.return_value=False
                handler.do_GET();handler.send_response.assert_called_with(403)
                agent.quorum_authority.valid.return_value=True;agent.runtime.mode.return_value='frozen'
                handler.do_GET();handler.send_response.assert_called_with(403)
                agent.runtime.mode.return_value='active';agent.config['enforce']=False
                handler.do_GET();handler.send_response.assert_called_with(403)
                agent.config['enforce']=True;agent.quorum_authority=None
                handler.do_GET();handler.send_response.assert_called_with(403)

    def test_heartbeat_fault_is_root_only_boot_bound_and_expires_within_180_seconds(self):
        import json,stat,uuid
        value=dict(version=1,testId=str(uuid.uuid4()),bootId='boot',startedBoot=100,untilBoot=280)
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'fault.json';path.write_text(json.dumps(value))
            info=types.SimpleNamespace(st_mode=stat.S_IFREG|0o600,st_uid=0,st_size=200)
            with patch.object(Path,'lstat',return_value=info):
                self.assertTrue(module.heartbeat_test_blocked(path,now=101,boot_id='boot'))
                self.assertFalse(module.heartbeat_test_blocked(path,now=280,boot_id='boot'))
                self.assertFalse(module.heartbeat_test_blocked(path,now=99,boot_id='boot'))
                self.assertFalse(module.heartbeat_test_blocked(path,now=101,boot_id='old-boot'))
                for change in ({'untilBoot':281},{'testId':'bad'},{'startedBoot':True}):
                    path.write_text(json.dumps({**value,**change}))
                    self.assertFalse(module.heartbeat_test_blocked(path,now=101,boot_id='boot'))
                path.write_text(json.dumps(value));info.st_uid=1000
                self.assertFalse(module.heartbeat_test_blocked(path,now=101,boot_id='boot'))
                info.st_uid=0;info.st_mode=stat.S_IFREG|0o644
                self.assertFalse(module.heartbeat_test_blocked(path,now=101,boot_id='boot'))
                info.st_mode=stat.S_IFLNK|0o600
                self.assertFalse(module.heartbeat_test_blocked(path,now=101,boot_id='boot'))

    def test_watchdog_retries_failed_fencing_even_after_origin_deadline_is_zeroed(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as root:
            agent=self.make_agent(root)
            agent.quorum_authority=Mock();agent.quorum_authority.state='admitted'
            agent.quorum_authority.valid.return_value=False
            agent.view=dict(role='active',owner='primary',epoch=14,deadline=0,ready=False)
            agent.fence=Mock(side_effect=[RuntimeError('systemd temporarily unavailable'),None])
            with patch.object(module.time,'sleep',side_effect=[None,StopIteration]),patch.object(module.logging,'error'):
                with self.assertRaises(StopIteration):agent.watch_grant()
            self.assertEqual(agent.fence.call_count,2)

    def test_cloudflare_grant_cannot_activate_when_quorum_denies_it(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as root:
            agent=self.make_agent(root)
            agent.quorum_authority=Mock()
            agent.quorum_authority.admit.side_effect=RuntimeError('lease-held')
            agent.activate=Mock();agent.fence=Mock()
            response={'role':'active','owner':'primary','epoch':5,'leaseMs':240000}
            with patch.object(module,'isolated',return_value=True),patch.object(module,'local_healthy',return_value=True),patch.object(module,'request_json',return_value=response):
                with self.assertRaisesRegex(RuntimeError,'lease-held'):agent.tick()
            agent.activate.assert_not_called()
            self.assertFalse(agent.view['ready'])

    def test_quorum_expiry_fences_postgres_even_if_cloudflare_still_names_this_owner(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as root:
            agent=self.make_agent(root)
            agent.runtime=Mock();agent.quorum_authority=Mock()
            agent.quorum_authority.valid.return_value=False
            agent.view=dict(role='active',owner='primary',epoch=14,deadline=1000,ready=True)
            agent.fence()
            agent.runtime.fence.assert_called_once_with(14,None)
            self.assertEqual(agent.view['deadline'],0)

    def test_origin_rejects_expired_quorum_while_cloudflare_lease_is_live(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as root:
            agent=self.make_agent(root)
            agent.quorum_authority=Mock();agent.quorum_authority.valid.return_value=False
            agent.view=dict(role='active',owner='primary',ready=True,deadline=1000,epoch=14)
            handler=object.__new__(module.handler_for(agent));handler.path='/authorize'
            handler.headers={'X-Immich-HA-Token':agent.config['origin_token'],'X-Immich-HA-Epoch':'14'}
            handler.send_response=Mock();handler.send_header=Mock();handler.end_headers=Mock()
            with patch.object(module.time,'monotonic',return_value=100):handler.do_GET()
            handler.send_response.assert_called_once_with(403)

    def test_shadow_failure_cannot_fence_or_prevent_cloudflare_activation(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as root:
            agent=self.make_agent(root)
            agent.quorum_shadow=Mock();agent.quorum_shadow.submit.side_effect=RuntimeError('quorum offline')
            agent.activate=Mock();agent.fence=Mock()
            response={'role':'active','owner':'primary','epoch':1,'leaseMs':240000}
            with patch.object(module,'isolated',return_value=True),patch.object(module,'local_healthy',return_value=True),patch.object(module,'request_json',return_value=response):
                agent.tick()
            agent.activate.assert_called_once();agent.fence.assert_not_called()
            self.assertTrue(agent.view['ready'])

    def test_origin_denies_frozen_runtime_even_with_unexpired_active_grant(self):
        from unittest.mock import Mock
        agent=Mock();agent.config={'origin_token':'private-origin-token'}
        agent.view={'role':'active','ready':True,'deadline':1000,'epoch':9}
        handler=object.__new__(module.handler_for(agent));handler.path='/authorize'
        handler.headers={'X-Immich-HA-Token':'private-origin-token','X-Immich-HA-Epoch':'9'}
        handler.send_response=Mock();handler.send_header=Mock();handler.end_headers=Mock()
        with patch.object(module.time,'monotonic',return_value=100):
            agent.runtime.mode.return_value='frozen';handler.do_GET();handler.send_response.assert_called_with(403)
            agent.runtime.mode.return_value='active';handler.do_GET();handler.send_response.assert_called_with(200)

    def setUp(self):
        # CI must not depend on an Immich Linux user existing on the runner.
        account = types.SimpleNamespace(pw_uid=999, pw_gid=1000, pw_dir='/opt/immich')
        user = patch.object(module.pwd, 'getpwnam', return_value=account)
        user.start()
        self.addCleanup(user.stop)

    def test_maintenance_grant_uses_quiescing_instead_of_ordinary_standby_fencing(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as root:
            agent = self.make_agent(root, "backup")
            agent.runtime = Mock()
            agent.view = {"role":"maintenance","epoch":5,"owner":"primary","deadline":0,"ready":False}
            agent.fence()
            agent.runtime.enter_maintenance.assert_called_once()
            agent.runtime.fence.assert_not_called()

    def make_agent(self, root, node="primary"):
        with patch.object(module, "STATE", Path(root)):
            return module.Agent({"node": node, "controller_url": "https://control.workers.dev",
                                 "agent_token": "a" * 48, "origin_token": "b" * 48, "enforce": True})

    def test_expired_primary_lease_stops_service_without_reversing_sync(self):
        with tempfile.TemporaryDirectory() as root:
            agent = self.make_agent(root)
            agent.was_owner = True
            agent.view = {"role": "active", "epoch": 1, "deadline": 0, "ready": True}
            with patch.object(module, "command") as cmd, patch.object(module, "pause_sync") as pause:
                agent.fence()
            self.assertFalse(agent.view["ready"])
            cmd.assert_called_once_with("systemctl", "stop", "immich-web.service", "immich-ml.service")
            pause.assert_not_called()

    def test_returning_primary_pauses_sync_even_if_already_fenced(self):
        with tempfile.TemporaryDirectory() as root:
            agent = self.make_agent(root)
            agent.was_owner = True
            agent.fenced = True
            agent.view = {"role": "standby", "epoch": 2, "deadline": 0, "ready": False}
            with patch.object(module, "command") as cmd, patch.object(module, "pause_sync") as pause:
                agent.fence()
            cmd.assert_not_called()
            pause.assert_called_once()

    def test_backup_guard_is_written_before_stopping_loader_and_pausing_sync(self):
        with tempfile.TemporaryDirectory() as root:
            agent = self.make_agent(root, "backup")
            events = []
            with patch.object(module.os.path, "ismount", return_value=True), \
                 patch.object(module, "atomic_text", side_effect=lambda *a: events.append(("write", str(a[0]), a[1]))), \
                 patch.object(module, "command", side_effect=lambda *a, **k: events.append(("command", *a))), \
                 patch.object(module, "pause_sync", side_effect=lambda *a: events.append(("pause",))):
                agent.activate()
            self.assertEqual(events[0][0], "write")
            self.assertEqual(events[0][2], "active")
            self.assertIn("immich-standby-update.timer", events[1])
            self.assertEqual(events[2], ("pause",))
            self.assertTrue(agent.activation_done)

    def test_promotion_failure_never_marks_activation_complete(self):
        with tempfile.TemporaryDirectory() as root:
            agent = self.make_agent(root, "backup")
            with patch.object(module.os.path, "ismount", return_value=True), \
                 patch.object(module, "atomic_text"), patch.object(module, "command"), \
                 patch.object(module, "pause_sync", side_effect=RuntimeError("pause failed")):
                with self.assertRaises(RuntimeError):
                    agent.activate()
            self.assertFalse(agent.activation_done)

    def test_observe_mode_never_runs_activation_commands(self):
        with tempfile.TemporaryDirectory() as root:
            agent = self.make_agent(root)
            agent.config["enforce"] = False
            answer = {"role": "active", "epoch": 1, "leaseMs": 90000}
            with patch.object(module, "local_healthy", return_value=True), \
                 patch.object(module, "request_json", return_value=answer), \
                 patch.object(agent, "activate") as activate, patch.object(agent, "fence") as fence:
                agent.tick()
            activate.assert_not_called()
            fence.assert_not_called()

    def test_enforcement_refuses_nonisolated_app(self):
        with tempfile.TemporaryDirectory() as root:
            agent = self.make_agent(root)
            with patch.object(module, "isolated", return_value=False), \
                 patch.object(agent, "fence") as fence, patch.object(module, "request_json") as request:
                with self.assertRaises(RuntimeError):
                    agent.tick()
            fence.assert_called_once()
            request.assert_not_called()

    def authorize(self, agent, token, epoch, now):
        events = []
        handler = module.handler_for(agent)
        fake = types.SimpleNamespace(
            path="/authorize", headers={"X-Immich-HA-Token": token, "X-Immich-HA-Epoch": str(epoch)},
            send_error=lambda status: events.append(status),
            send_response=lambda status: events.append(status),
            send_header=lambda *args: None, end_headers=lambda: None)
        with patch.object(module.time, "monotonic", return_value=now):
            handler.do_GET(fake)
        return events[0]

    def test_origin_gate_rejects_expired_grant_without_contacting_cloudflare(self):
        with tempfile.TemporaryDirectory() as root:
            agent = self.make_agent(root)
            agent.view = {"role": "active", "epoch": 1, "deadline": 100, "ready": True}
            self.assertEqual(self.authorize(agent, "b" * 48, 1, 101), 403)

    def test_origin_gate_rejects_wrong_secret_and_stale_epoch(self):
        with tempfile.TemporaryDirectory() as root:
            agent = self.make_agent(root)
            agent.view = {"role": "active", "epoch": 2, "deadline": 100, "ready": True}
            self.assertEqual(self.authorize(agent, "wrong", 2, 50), 403)
            self.assertEqual(self.authorize(agent, "b" * 48, 1, 50), 403)
            self.assertEqual(self.authorize(agent, "b" * 48, 2, 50), 200)

    def test_origin_gate_stays_closed_until_activation_finishes(self):
        with tempfile.TemporaryDirectory() as root:
            agent = self.make_agent(root)
            agent.view = {"role": "active", "epoch": 2, "deadline": 100, "ready": False}
            self.assertEqual(self.authorize(agent, "b" * 48, 2, 50), 403)


if __name__ == "__main__":
    unittest.main()
