import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('native_backup_sync',Path(__file__).parents[1]/'tools/sync-native-backups.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


class NativeBackupSyncTests(unittest.TestCase):
    def test_rules_keep_local_files_and_excluded_downloads_private(self):
        existing=['/icloud-download','/lost+found','/library/.immich','/backups']
        rules=m.rules(existing)
        self.assertNotIn('/backups',rules)
        for value in existing[:-1]:self.assertIn(value,rules)
        self.assertLess(rules.index('/backups/pg-stream'),rules.index('!/backups/immich-db-backup-*.sql.gz'))
        self.assertLess(rules.index('/backups/postgres'),rules.index('!/backups/immich-db-backup-*.sql.gz'))
        self.assertLess(rules.index('!/backups/immich-db-backup-*.sql.gz'),rules.index('/backups/*'))
        self.assertNotIn('!/backups/**',rules)

    def test_reapplying_is_idempotent_without_duplicate_rules(self):
        first=m.rules(['/backups','/icloud-download'])
        self.assertEqual(m.rules(first),first)
        self.assertEqual(len(first),len(set(first)))


if __name__=='__main__':unittest.main()
