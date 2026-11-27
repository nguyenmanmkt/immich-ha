import ast
from pathlib import Path
import stat
import sys
import types
import unittest
from unittest.mock import Mock, patch

import quorum_setup as q


class MarkerAccessTests(unittest.TestCase):
    def fixture(self):
        base=Mock(name='config-directory');marker=Mock(name='marker');private=Mock(name='private-config')
        base.__truediv__=Mock(return_value=marker)
        for item,mode in ((base,stat.S_IFDIR|0o700),(marker,stat.S_IFREG|0o644),(private,stat.S_IFREG|0o600)):
            item.stat.return_value=types.SimpleNamespace(st_uid=0,st_gid=0,st_mode=mode)
            item.is_symlink.return_value=False
        marker.is_dir.return_value=False;private.is_dir.return_value=False
        base.iterdir.return_value=[marker,private]
        namespace={};exec(q.APP_MARKER_ACCESS,namespace)
        return base,marker,private,namespace['ensure_app_marker_access']

    def run_fixture(self,base,callback):
        user=types.SimpleNamespace(pw_uid=999,pw_gid=991)
        with patch.dict(sys.modules,{'pwd':types.SimpleNamespace(getpwnam=lambda _:user)}), \
             patch('pathlib.Path',return_value=base), \
             patch('os.getgrouplist',return_value=[991],create=True), \
             patch('os.chown',create=True) as chown,patch('os.chmod') as chmod, \
             patch('subprocess.run') as run:
            callback()
            return chown,chmod,run

    def test_app_can_traverse_but_directory_listing_and_private_modes_are_not_granted(self):
        base,marker,private,callback=self.fixture()
        chown,chmod,run=self.run_fixture(base,callback)
        chown.assert_called_once_with(base,0,991)
        chmod.assert_called_once_with(base,0o710)
        run.assert_called_once_with(['runuser','-u','immich','--','test','-r',str(marker)],check=True,capture_output=True)
        private.chmod.assert_not_called()

    def test_readable_private_config_aborts_before_changing_permissions(self):
        base,marker,private,callback=self.fixture()
        private.stat.return_value.st_mode=stat.S_IFREG|0o644
        with self.assertRaisesRegex(RuntimeError,'would become readable'):
            self.run_fixture(base,callback)

    def test_symlink_and_non_root_marker_are_rejected(self):
        for kind in ('symlink','owner'):
            with self.subTest(kind=kind):
                base,marker,private,callback=self.fixture()
                if kind=='symlink':marker.is_symlink.return_value=True
                else:marker.stat.return_value.st_uid=999
                with self.assertRaisesRegex(RuntimeError,'Unsafe quorum marker'):
                    self.run_fixture(base,callback)

    def test_embedded_install_payloads_compile_and_apply_access_before_restart(self):
        root=Path(__file__).resolve().parents[1]
        for name in ('quorum-enforce.py','quorum-fallback.py'):
            tree=ast.parse((root/'tools'/name).read_text(encoding='utf-8'))
            assignment=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='ENABLE' for t in n.targets))
            payload=eval(compile(ast.Expression(assignment.value),name,'eval'),{'q':q})
            compile(payload,name+' embedded','exec')
            self.assertLess(payload.rindex('ensure_app_marker_access()'),payload.index("['systemctl','restart'"))
