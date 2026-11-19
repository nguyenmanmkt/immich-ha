import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

try:
    import pwd
except ModuleNotFoundError:
    sys.modules["pwd"] = types.SimpleNamespace(getpwnam=lambda _: types.SimpleNamespace(pw_uid=999, pw_gid=991))
spec = importlib.util.spec_from_file_location("media_access_test", Path(__file__).parents[1] / "node/ha_runtime.py")
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


class MediaAccessTests(unittest.TestCase):
    def setUp(self):
        # CI must not depend on an Immich Linux user existing on the runner.
        account = types.SimpleNamespace(pw_uid=999, pw_gid=1000, pw_dir='/opt/immich')
        user = patch.object(module.pwd, 'getpwnam', return_value=account)
        user.start()
        self.addCleanup(user.stop)

    def test_snapshot_excludes_only_local_root_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'library/user').mkdir(parents=True)
            for name in module.MANAGED:
                (root/name).mkdir(exist_ok=True)
            (root/'library/.immich').write_bytes(b'local-marker')
            (root/'library/user/.immich').write_bytes(b'ordinary-nested-file')
            (root/'library/photo.jpg').write_bytes(b'original-photo')
            with patch.object(module,'MEDIA',root),patch.object(module,'ROOT',root/'state'):
                runtime=module.Runtime({'node':'primary'},None,None,None)
                names={entry['path'] for entry in runtime.manifest()}
            self.assertNotIn('library/.immich',names)
            self.assertIn('library/user/.immich',names)
            self.assertIn('library/photo.jpg',names)

    def test_repairs_received_files_and_markers_without_touching_other_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ("library", "encoded-video", "icloud-download", "lost+found", "backups/recovery/private"):
                (root/name).mkdir(parents=True)
            photo=root/'library/photo.jpg';photo.write_bytes(b'original-photo-bytes')
            (root/'encoded-video/.immich').write_bytes(b'immich-marker')
            private=root/'backups/recovery/private/old.dump';private.write_bytes(b'private-archive')
            chown=Mock(); result=module.ensure_media_access(root,123456,123457,chown=chown)
            changed={call.args[0].relative_to(root).as_posix() for call in chown.call_args_list}
            self.assertIn('library/photo.jpg',changed)
            self.assertIn('encoded-video/.immich',changed)
            self.assertFalse(any(path.startswith(('icloud-download','lost+found','backups/recovery')) for path in changed))
            self.assertEqual(photo.read_bytes(),b'original-photo-bytes')
            self.assertEqual(private.read_bytes(),b'private-archive')
            self.assertGreater(result['ownershipChanged'],0)

    def test_symlink_root_is_rejected_without_chown(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Path,'is_symlink',return_value=True):
            chown=Mock()
            with self.assertRaises(RuntimeError): module.ensure_media_access(Path(directory),1,1,chown=chown)
            chown.assert_not_called()
