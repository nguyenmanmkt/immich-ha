import unittest
from ssh_nodes import settings


class SSHSettingsTests(unittest.TestCase):
    def setUp(self):
        self.project = {"nodes": {"primary": {"ip": "192.0.2.10"}, "backup": {"ip": "192.0.2.20"}}}

    def test_node_credentials_are_separate_and_password_is_literal(self):
        env = {"SSH_PRIMARY_PASSWORD": 'pa$$#word "quoted"', "SSH_BACKUP_PASSWORD": "backup-pass"}
        first = settings("primary", env, self.project)
        second = settings("backup", env, self.project)
        self.assertEqual(first["password"], 'pa$$#word "quoted"')
        self.assertEqual(second["password"], "backup-pass")
        self.assertEqual(first["hostname"], "192.0.2.10")
        self.assertEqual(second["hostname"], "192.0.2.20")

    def test_invalid_target_fields_are_rejected(self):
        for env in ({"SSH_PRIMARY_HOST": "host;command"}, {"SSH_PRIMARY_PORT": "70000"}, {"SSH_PRIMARY_USER": "root;command"}):
            with self.assertRaises(ValueError):
                settings("primary", env, self.project)
