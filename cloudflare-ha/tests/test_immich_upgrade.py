import contextlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).parents[1]/'node'))
import immich_upgrade as u

class UpgradeTests(unittest.TestCase):
    def test_native_path_rewrite_skips_package_directories_ending_with_js(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);package=root/'node_modules/ipaddr.js';package.mkdir(parents=True)
            file=package/'index.js';file.write_text("const a='/usr/src/resources'; const b='/build';")
            u.adapt_native_paths(root)
            self.assertIn('/opt/immich/resources',file.read_text())
            self.assertIn('/opt/immich/app',file.read_text())

    def test_private_staging_exclusion_preserves_all_existing_ignore_rules(self):
        r=self.runtime();step=u.Upgrader(r,self.command(),Mock())
        old=['!/backups/immich-db-backup-*.sql.gz','/icloud-download','/lost+found']
        sync=Mock();sync.folder.return_value={'type':'sendonly','paused':False};sync.api.side_effect=[{'ignore':old},None,{'ignore':['/backups/immich-upgrades']+old}]
        with patch('ha_runtime.Syncthing',return_value=sync):step.protect_staging()
        self.assertEqual(sync.api.call_args_list[1].args[1],{'ignore':['/backups/immich-upgrades']+old})

    def command(self):
        return {'jobId':'12345678-1234-1234-1234-123456789abc','epoch':5,'source':'primary','action':'upgrade-freeze',
          'upgradeBackup':{'verified':True,'sha256':'a'*64,'version':'3.4.2','replicaSha256':'a'*64},
          'upgradeBackupCopy':{'copied':True,'sha256':'a'*64},
          'upgrade':{'stageId':'12345678-1234-1234-1234-123456789abc','version':'3.4.2','recipe':'native-3.4.2-r1','source':'primary','epoch':5,'systemId':'123'}}

    def runtime(self):
        r=Mock();r.node='primary';r.lock.return_value=contextlib.nullcontext();r.read.return_value=''
        r.guard.return_value={'operation':{'id':self.command()['jobId'],'status':'running'}}
        return r

    def test_only_reviewed_recipe_and_current_authority_are_accepted(self):
        c=self.command();c['upgrade']['version']='3.5.0'
        with self.assertRaises(ValueError):u.Upgrader(self.runtime(),c,Mock())
        c=self.command();c['upgrade']['epoch']=4
        with self.assertRaises(ValueError):u.Upgrader(self.runtime(),c,Mock())
        c=self.command();c['upgrade']['stageId']='../../elsewhere'
        with self.assertRaises(ValueError):u.Upgrader(self.runtime(),c,Mock())

    def test_stale_job_is_rejected_before_any_mutation(self):
        r=self.runtime();r.guard.return_value={'operation':{'id':'other','status':'running'}}
        with self.assertRaises(RuntimeError):u.Upgrader(r,self.command(),Mock()).execute()
        r.write.assert_not_called()

    def test_freeze_persists_activation_block_before_stopping_app(self):
        r=self.runtime();step=u.Upgrader(r,self.command(),Mock());calls=[]
        r.write.side_effect=lambda *args:calls.append(args)
        with patch.object(step,'verify'),patch.object(u,'sync_activation_block'),patch.object(step,'run',side_effect=lambda args,**kw:calls.append(args)),patch.object(u.subprocess,'run',return_value=Mock(returncode=3)):
            result=step.freeze()
        self.assertEqual(calls[0][0],u.LOCK_NAME)
        self.assertEqual(calls[1],('mode','upgrade-frozen'))
        self.assertEqual(calls[2][0],'systemctl');self.assertTrue(result['appStopped'])

    def test_active_install_requires_durable_freeze(self):
        r=self.runtime();step=u.Upgrader(r,self.command(),Mock())
        with patch.object(step,'verify',return_value={'sha256':'a'*64}),patch.object(step,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'khóa active'):step.install()
            run.assert_not_called()

    def test_install_requires_verified_backup_on_second_machine(self):
        r=self.runtime();r.read.return_value='blocked';command=self.command();command.pop('upgradeBackupCopy')
        step=u.Upgrader(r,command,Mock())
        with patch.object(step,'verify',return_value={'sha256':'a'*64}),patch.object(step,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'backup trên cả hai'):step.install()
            run.assert_not_called()

    def test_retry_preserves_existing_dump_after_interrupted_backup_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            r=self.runtime();r.read.return_value='blocked';step=u.Upgrader(r,self.command(),Mock());step.folder=Path(folder)
            original=step.folder/'before-upgrade.dump';original.write_bytes(b'original pre-upgrade backup')
            with patch.object(step,'run',return_value='valid dump TOC') as run,patch.object(step,'export_backup',return_value={'ok':True}),patch.object(u,'fsync_dir'):
                self.assertEqual(step.backup(),{'ok':True})
            self.assertEqual(original.read_bytes(),b'original pre-upgrade backup')
            self.assertEqual(run.call_args.args[0][0],'pg_restore')
            self.assertTrue((step.folder/'backup.json').is_file())

    def test_backup_requires_freeze_and_does_not_auto_restore(self):
        step=u.Upgrader(self.runtime(),self.command(),Mock())
        with self.assertRaisesRegex(RuntimeError,'đóng ứng dụng'):step.backup()

    def test_missing_or_expired_grant_never_starts_new_app(self):
        r=self.runtime();r.grant={'role':'active','epoch':5,'deadline':0}
        step=u.Upgrader(r,self.command(),Mock())
        with patch.object(step,'guard'),patch.object(step,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'lease'):step.start()
            run.assert_not_called()

    def test_tree_hash_detects_code_changes_and_ignores_python_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'code.py').write_text('old')
            before=u.tree_hash(root)
            (root/'__pycache__').mkdir();(root/'__pycache__/code.pyc').write_bytes(b'cache')
            self.assertEqual(before,u.tree_hash(root))
            (root/'code.py').write_text('new');self.assertNotEqual(before,u.tree_hash(root))

    def test_activation_guard_fails_closed_on_malformed_persistent_marker(self):
        r=self.runtime();r.read.return_value='{broken'
        self.assertTrue(u.blocked(r))

    def test_start_publishes_fresh_grant_before_unblocking_and_systemd(self):
        r=self.runtime();r.grant={'role':'active','owner':'primary','epoch':5,'deadline':200}
        step=u.Upgrader(r,self.command(),Mock());events=[]
        r.write.side_effect=lambda *args:events.append(('write',*args))
        with tempfile.TemporaryDirectory() as folder:
            step.folder=Path(folder);dump=step.folder/'before-upgrade.dump';dump.write_bytes(b'verified backup')
            (step.folder/'backup.json').write_text(json.dumps({'verified':True,'sha256':u.hashlib.sha256(dump.read_bytes()).hexdigest()}))
            with patch('pg_stream.status',return_value={'inRecovery':False}),patch.object(u,'version',return_value='3.4.2'), \
                    patch.object(u.time,'monotonic',return_value=100),patch('ha_runtime.atomic',side_effect=lambda *args:events.append(('grant',*args))), \
                    patch.object(u,'sync_activation_block'),patch.object(step,'run',side_effect=lambda *args,**kw:events.append(('run',*args))):
                self.assertTrue(step.start()['started'])
        self.assertEqual(events[0][0],'grant');self.assertEqual(events[0][2],'200')
        self.assertEqual(events[1],('write','mode','active'))
        self.assertEqual(events[2],('write',u.LOCK_NAME,''))
        self.assertEqual(events[3][0],'run');r.wait_healthy.assert_called_once()

    def test_rebooted_source_needs_same_lineage_and_live_grant_before_pg_start(self):
        r=self.runtime();r.grant={'role':'active','owner':'primary','epoch':5,'deadline':200}
        step=u.Upgrader(r,self.command(),Mock())
        with tempfile.TemporaryDirectory() as folder,patch('pg_stream.DATA',Path(folder)), \
                patch('pg_stream.status',side_effect=[RuntimeError('down'),{'inRecovery':False,'systemId':'123'}]), \
                patch('pg_stream.ctl') as ctl,patch.object(step,'run',return_value='Database system identifier: 123'), \
                patch.object(u.time,'monotonic',return_value=100),patch('ha_runtime.Syncthing') as sync:
            sync.return_value.folder.return_value={'type':'sendonly','paused':False}
            step.ensure_postgres();ctl.assert_called_once_with('start')

    def test_rebooted_source_without_lease_or_target_without_standby_signal_never_starts_pg(self):
        for node in ['primary','backup']:
            r=self.runtime();r.node=node;r.grant={};step=u.Upgrader(r,self.command(),Mock())
            with tempfile.TemporaryDirectory() as folder,patch('pg_stream.DATA',Path(folder)),patch('pg_stream.status',side_effect=RuntimeError('down')),patch('pg_stream.ctl') as ctl:
                with self.assertRaises(RuntimeError):step.ensure_postgres()
                ctl.assert_not_called()

    def test_rebooted_replica_starts_only_readonly_and_rejects_different_pgdata(self):
        r=self.runtime();r.node='backup';step=u.Upgrader(r,self.command(),Mock())
        with tempfile.TemporaryDirectory() as folder,patch('pg_stream.DATA',Path(folder)),patch('pg_stream.status',side_effect=RuntimeError('down')),patch('pg_stream.ctl') as ctl,patch.object(step,'run',return_value='Database system identifier: 999'):
            (Path(folder)/'standby.signal').touch()
            with self.assertRaisesRegex(RuntimeError,'dòng database'):step.ensure_postgres()
            ctl.assert_not_called()
