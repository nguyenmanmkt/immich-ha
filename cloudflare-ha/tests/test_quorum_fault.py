import contextlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
import json
import quorum_stage as stage

spec=importlib.util.spec_from_file_location('quorum_witness_test',Path(__file__).parents[1]/'tools/quorum-test-witness.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class WitnessFaultTests(unittest.TestCase):
    def test_failed_write_still_restores_witness_and_removes_only_the_test_key(self):
        plan={'clusterId':'12345678-1234-1234-1234-123456789abc',
            'members':{n:{'tailscaleIp':'100.64.0.'+str(i+1)} for i,n in enumerate(('primary','backup','witness'))}}
        healthy={'owner':'backup','epoch':14,'ready':True,'standbyEligible':True,'auto':True,'operation':{'status':'succeeded'}}
        clients={n:Mock(name=n) for n in plan['members']};actions=[]
        def remote(client,payload,data,**kwargs):
            if payload==stage.CHECK:return {}
            actions.append((client,data['action']))
            if payload==stage.SMOKE and data['action']=='put':raise RuntimeError('simulated failed write')
            if payload==stage.SMOKE:self.assertIn('/checks/',data['key'])
            return {'ok':True}
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'plan.json';path.write_text(json.dumps(plan))
            with patch.object(module.q,'PLAN',path),patch.object(module.stage,'validate_plan'), \
                patch.object(module.stage,'validate_check'),patch.object(module.d,'control',return_value=healthy), \
                patch.object(module.q,'connections',return_value=contextlib.nullcontext((clients,{}))), \
                patch.object(module,'load_env',return_value={'SSH_COORDINATOR_HOST':'100.64.0.3'}), \
                patch.object(module.d,'remote',side_effect=remote),patch('builtins.print'):
                with self.assertRaisesRegex(RuntimeError,'simulated failed write'):module.main()
        self.assertIn((clients['witness'],'stop'),actions)
        self.assertIn((clients['witness'],'restore'),actions)
        self.assertIn((clients['primary'],'delete'),actions)
        self.assertLess(actions.index((clients['witness'],'restore')),actions.index((clients['primary'],'delete')))

    def test_recovery_payload_is_valid_python_with_bounded_restore_timer(self):
        compile(module.WITNESS,'witness fault-test SSH payload','exec')
        self.assertIn('--on-active=120s',module.WITNESS)
        self.assertNotIn('immich-ha-app',module.WITNESS)
        self.assertNotIn('pct stop',module.WITNESS)


if __name__=='__main__':unittest.main()
