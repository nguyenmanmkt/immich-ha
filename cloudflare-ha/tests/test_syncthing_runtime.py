import contextlib
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

try:
    import pwd
except ModuleNotFoundError:
    sys.modules["pwd"] = types.SimpleNamespace(getpwnam=lambda _: types.SimpleNamespace(pw_gid=1000))
spec = importlib.util.spec_from_file_location("syncthing_runtime_test", Path(__file__).parents[1] / "node/ha_runtime.py")
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


class FakeSync(module.Syncthing):
    def __init__(self):
        self.folders = {folder: {"type": "sendonly", "paused": False, "devices": [{"deviceID": "peer"}]} for folder in ("immich-library", "immich-database")}
        self.calls = []

    def folder(self, folder):
        return dict(self.folders[folder])

    def api(self, path, data=None, method=None, timeout=30):
        self.calls.append((path, method, timeout))
        if path.startswith('/rest/db/ignores'):
            return {'ignore':['/'+name for name in sorted(module.MARKERS)]}
        if method == "PATCH": self.folders[path.rsplit("/", 1)[1]].update(data)


class SyncthingTests(unittest.TestCase):
    def test_marker_exclusions_preserve_existing_rules_and_are_exact(self):
        sync = FakeSync()
        existing = ['/icloud-download', '/backups', '/lost+found', '!keep-this']
        with patch.object(sync,'api',side_effect=[{'ignore':existing},{}]) as api:
            sync.exclude_local_markers()
        path,body,method=api.call_args.args
        self.assertEqual(method,'POST')
        self.assertEqual(body['ignore'][-4:],existing)
        self.assertEqual(set(body['ignore'][:5]),{'/'+name for name in module.MARKERS})
        self.assertNotIn('**/.immich',body['ignore'])

    def test_retry_does_not_pause_an_already_correct_folder(self):
        sync = FakeSync(); messages = []
        sync.configure("sendonly", "peer", messages.append)
        self.assertFalse(any(method == "PATCH" for _, method, _ in sync.calls))
        scans = [timeout for path, _, timeout in sync.calls if path.startswith("/rest/db/scan?")]
        self.assertEqual(scans, [1200, 1200])
        self.assertTrue(any("immich-library" in message for message in messages))
        self.assertTrue(any("immich-database" in message for message in messages))

    def test_changing_a_paused_folder_does_not_repeat_pause(self):
        sync = FakeSync()
        sync.folders["immich-library"].update(type="receiveonly", paused=True)
        sync.configure("sendonly", "peer")
        patches = [call for call in sync.calls if call[1] == "PATCH"]
        self.assertEqual(len(patches), 1)
        self.assertEqual(sync.folders["immich-library"]["type"], "sendonly")
        self.assertFalse(sync.folders["immich-library"]["paused"])

    def test_authority_loss_after_scan_prevents_override(self):
        sync = FakeSync(); count = 0
        def guard():
            nonlocal count
            if any(path.startswith("/rest/db/scan?") for path, _, _ in sync.calls):
                raise RuntimeError("ownership changed")
        with self.assertRaisesRegex(RuntimeError, "ownership changed"):
            sync.configure("sendonly", "peer", guard=guard)
        self.assertFalse(any("override" in path for path, _, _ in sync.calls))

    def test_api_timeout_has_endpoint_context_without_api_key(self):
        sync = module.Syncthing({}, lambda _: ("http://127.0.0.1:8384", "private-api-key"))
        with patch.object(module.urllib.request, "urlopen", side_effect=TimeoutError()):
            with self.assertRaises(RuntimeError) as error:
                sync.scan("immich-library")
        self.assertIn("POST /rest/db/scan?folder=immich-library", str(error.exception))
        self.assertIn("1200", str(error.exception))
        self.assertNotIn("private-api-key", str(error.exception))
