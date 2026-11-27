import builtins
import importlib.util
from pathlib import Path
import symtable
import unittest
from unittest.mock import Mock, patch

import deployment as d
import streaming_setup


class DeploymentTests(unittest.TestCase):
    def test_importing_operational_entrypoints_does_not_connect_or_mutate(self):
        with patch.object(d,'connection') as connection,patch.object(d,'control') as control:
            for name in ['streaming-stage-test.py','streaming-finalize.py','upgrade-immich-ha.py','quorum-preflight.py','quorum-stage.py','quorum-test-witness.py','quorum-shadow.py','quorum-tls-compat.py','quorum-enforce.py','quorum-fallback.py','quorum-test-cloudflare.py','install-local-gateway.py']:
                spec=importlib.util.spec_from_file_location('entrypoint_test',d.ROOT/'tools'/name)
                module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
                self.assertTrue(callable(module.main))
            connection.assert_not_called();control.assert_not_called()

    def test_direct_connection_does_not_need_a_private_jump_helper(self):
        client=Mock()
        with patch.object(d,'load_env',return_value={}),patch.object(d,'load_project'), \
                patch.object(d,'settings',return_value={'hostname':'node','port':22}),patch.object(d,'connect',return_value=client) as connect:
            self.assertEqual(d.connection('primary'),(client,None))
            connect.assert_called_once()

    def test_failed_jump_channel_closes_gateway(self):
        gateway=Mock();gateway.get_transport.return_value.open_channel.side_effect=OSError('channel failed')
        with patch.object(d,'load_env',return_value={'SSH_JUMP_HOST':'192.0.2.30'}),patch.object(d,'load_project'), \
                patch.object(d,'settings',return_value={'hostname':'node','port':22}), \
                patch.object(d,'connect',side_effect=[OSError('unreachable'),gateway]):
            with self.assertRaises(OSError):d.connection('primary')
            gateway.close.assert_called_once()

    def test_generic_jump_settings_keep_credentials_separate(self):
        options,known=d.jump_settings({'SSH_JUMP_HOST':'192.0.2.30','SSH_JUMP_PASSWORD':'jump-secret','SSH_PRIMARY_PASSWORD':'node-secret'})
        self.assertEqual(options['password'],'jump-secret');self.assertEqual(options['hostname'],'192.0.2.30')
        self.assertIsNone(known)
        self.assertIsNone(d.jump_settings({}))

    def test_embedded_migration_payloads_compile_and_import_their_global_dependencies(self):
        # Compiling a wrapper alone misses unresolved names inside SSH payloads.
        for name in ['PREFLIGHT','STAGE','COPY']:
            script=getattr(streaming_setup,name)
            compile(script,name,'exec')
            table=symtable.symtable(script,name,'exec')
            missing=[s.get_name() for s in table.get_symbols() if s.is_referenced() and not s.is_assigned()
                     and not s.is_imported() and s.get_name()!='INPUT' and not hasattr(builtins,s.get_name())]
            self.assertEqual(missing,[],name)
