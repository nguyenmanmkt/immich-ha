import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('source_audit_test',Path(__file__).resolve().parents[1]/'tools/prepare-github.py')
audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)


class PublishAuditTests(unittest.TestCase):
    def test_forced_tracked_credentials_and_host_pins_are_rejected(self):
        for name in ('cloudflare-ha/.dev.vars.production','keys/id_ed25519.pub','keys/id_rsa','keys/known_hosts','keys/authorized_keys','keys/client.ppk','keys/cert.pfx','keys/.pgpass','cloudflare-ha/generated/report.json'):
            with self.subTest(name=name):self.assertTrue(audit.private_path(name))

    def test_example_config_and_public_source_remain_shareable(self):
        for name in ('cloudflare-ha/.env.example','cloudflare-ha/project.example.json','cloudflare-ha/ARCHITECTURE.vi.md','cloudflare-ha/node/witness_funnel.py'):
            with self.subTest(name=name):self.assertFalse(audit.private_path(name))
