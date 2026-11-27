import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

path=Path(__file__).parents[1]/'tools/install-witness-funnel.py'
spec=importlib.util.spec_from_file_location('witness_funnel_test',path)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


class WitnessFunnelTests(unittest.TestCase):
    def checks(self,**change):
        return [dict(status=200,body='{"res":"pong"}',node=change.get('node','backup'),epoch=change.get('epoch','14')),
                dict(status=change.get('private',401),body='{}'),dict(status=404,body=''),dict(status=404,body='')]

    def test_common_url_must_point_to_the_expected_quorum_owner(self):
        with patch.object(m.f,'public_addresses',return_value=['8.8.8.8']),patch.object(m.f,'public_get',side_effect=self.checks()):
            result=m.check_public('witness.example.ts.net',443,'backup',14)
            self.assertEqual(result['checks']['ping']['node'],'backup')
        for change in ({'node':'primary'},{'epoch':'13'},{'private':200}):
            with patch.object(m.f,'public_addresses',return_value=['8.8.8.8']),patch.object(m.f,'public_get',side_effect=self.checks(**change)):
                with self.assertRaises(RuntimeError):m.check_public('witness.example.ts.net',443,'backup',14)

    def test_remote_status_payload_compiles_without_operational_import(self):
        compile(m.STATUS,'witness funnel status','exec')
        self.assertTrue(callable(m.main))
