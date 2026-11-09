"""Scenario tags must fail clearly rather than use a wrong engine/version."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock
from types import SimpleNamespace
import json
from k3s_backup import profiles
from k3s_backup.config import Config
from k3s_backup.errors import BackupError

class ProfileTests(TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.profile=self.root/'profile.json';self.patcher=mock.patch.object(profiles,'PROFILE_PATH',self.profile);self.patcher.start();self.addCleanup(self.patcher.stop)
    def save(self,p,pg=(),maria=()):
        self.profile.write_text(json.dumps({'schema_version':1,'profile':p,'files':True,'postgres_majors':list(pg),'mariadb_series':list(maria)}))
    def test_files_has_no_db_but_can_run_files(self):
        self.save('files');profiles.check_engine('files')
        with self.assertRaises(BackupError) as e:profiles.check_engine('postgres','17')
        self.assertEqual(e.exception.code,'image_profile_mismatch')
    def test_pg16_probes_16_and_not_missing_17(self):
        self.save('pg16',['16'])
        self.assertEqual(profiles.pg_probe_major(SimpleNamespace(target_major=None)),'16')
        self.assertEqual(profiles.pg_probe_major(SimpleNamespace(target_major='16')),'16')
        with self.assertRaises(BackupError):profiles.check_engine('postgres','17')
    def test_maria_does_not_claim_other_release_series(self):
        self.save('mariadb11.8',maria=['11.8']);profiles.check_engine('mariadb','11.8')
        with self.assertRaises(BackupError):profiles.check_engine('mariadb','11.5')
    def test_config_rejects_wrong_label_before_export(self):
        self.save('files')
        with self.assertRaises(BackupError):Config.from_env({'RESTIC_REPOSITORY':'/synthetic/repo','DATA_PATH':'/synthetic/data','BACKUP_WORK_PATH':'/synthetic/work','DATABASE_TYPE':'pg','DATABASE_NAME':'fixture','DATABASE_USER':'u'})
    def test_invalid_capabilities_fail_closed(self):
        self.profile.write_text('{}')
        with self.assertRaises(BackupError):profiles.capabilities()
    def test_source_checkout_without_metadata_is_explicit_developer_mode(self):
        self.assertIsNone(profiles.capabilities())
