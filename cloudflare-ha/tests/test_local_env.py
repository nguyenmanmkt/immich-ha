import json
from pathlib import Path
import tempfile
import unittest
from local_env import load_env, origin_credentials


class EnvTests(unittest.TestCase):
    def read(self, text, environment=None):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text(text, encoding="utf-8")
            return load_env(path, environment or {})

    def test_literal_secrets_and_environment_override(self):
        values = self.read('# comment\nTOKEN="abc#${SECRET}$(echo hi)"\nOTHER=\'literal#value\'\n', {"TOKEN": "process-value"})
        self.assertEqual(values["TOKEN"], "process-value")
        self.assertEqual(values["OTHER"], "literal#value")
        self.assertEqual(self.read('TOKEN="abc#${SECRET}$(echo hi)"')["TOKEN"], 'abc#${SECRET}$(echo hi)')

    def test_windows_paths_and_password_backslashes_are_literal(self):
        path = r'C:\Users\Lenovo\.ssh\id_ed25519'
        values = self.read('KEY="' + path + '"\nPASSWORD="literal\\nheader"')
        self.assertEqual(values["KEY"], path)
        self.assertEqual(values["PASSWORD"], r'literal\nheader')
        with self.assertRaises(ValueError) as error:
            self.read('TOKEN="secret\nheader"')
        self.assertNotIn("secret", str(error.exception))

    def test_supplied_service_pair_cannot_mix_with_saved_secret(self):
        saved = {"ADMIN_TOKEN": "keep", "ORIGIN_ACCESS_CLIENT_ID": "old.access", "ORIGIN_ACCESS_CLIENT_SECRET": "o" * 48}
        with self.assertRaises(ValueError):
            origin_credentials(saved, {"ORIGIN_ACCESS_CLIENT_ID": "new.access"})
        result = origin_credentials(saved, {"ORIGIN_ACCESS_CLIENT_ID": "new.access", "ORIGIN_ACCESS_CLIENT_SECRET": "n" * 48})
        self.assertEqual(result["ADMIN_TOKEN"], "keep")
        self.assertEqual(saved["ORIGIN_ACCESS_CLIENT_ID"], "old.access")
