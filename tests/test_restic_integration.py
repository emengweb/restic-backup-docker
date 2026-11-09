"""Real restic on synthetic local repos. No K8s, credentials or production datasets."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
import json
import os
import shutil
import subprocess
from k3s_backup.config import Config
from k3s_backup.runner import Runner
from k3s_backup.errors import BackupError

RESTIC = os.environ.get("RESTIC_BIN") or shutil.which("restic")

@skipUnless(RESTIC, "NOT RUN: restic executable is not available")
class ResticIntegration(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = dict(os.environ, RESTIC_REPOSITORY=str(self.root/'repo'), RESTIC_PASSWORD='synthetic-only',
                        DATASET_ID='synthetic-dataset', DATA_PATH=str(self.root/'data'), BACKUP_WORK_PATH=str(self.root/'scratch'),
                        RESTIC_BIN=str(RESTIC), BACKUP_MIN_FREE_BYTES='1', JOB_TIMEOUT_SECONDS='30')
        (self.root/'data').mkdir()
        r = subprocess.run([RESTIC, 'init'], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(r.returncode,0,'synthetic repo init failed')

    def test_latest_survives_changed_node_pod_and_empty_volume(self):
        c=Config.from_env(self.env); src=c.data_path
        (src/'nested').mkdir();(src/'nested'/'fixture').write_bytes(b'first')
        first=Runner(c).backup();self.assertEqual(first['status'],'backed_up')
        (src/'nested'/'fixture').write_bytes(b'newest')
        second=Runner(c).backup();self.assertEqual(second['status'],'backed_up')
        dest=self.root/'new-empty-volume';dest.mkdir()
        e=dict(self.env,DATA_PATH=str(dest),BACKUP_WORK_PATH=str(self.root/'scratch2'),HOSTNAME='different-pod',NODE_NAME='different-node')
        result=Runner(Config.from_env(e)).restore()
        self.assertEqual(result['status'],'restored')
        self.assertEqual((dest/'nested'/'fixture').read_bytes(),b'newest')
        self.assertEqual(result['snapshot_id'][:8],second['snapshot_id'][:8])
        checked=Runner(c).check();self.assertEqual(checked['scope'],'whole_repository')

    def test_source_is_not_copied_to_backup_work(self):
        c=Config.from_env(self.env);(c.data_path/'fixture').write_bytes(b'not-duplicated')
        Runner(c).backup()
        self.assertEqual(sorted(x.name for x in (c.work_path/'current').iterdir()),['backup-manifest.json'])

    def test_nonempty_data_is_preserved_even_when_repo_unreachable(self):
        c=Config.from_env(self.env);(c.data_path/'keep').write_bytes(b'existing')
        e=dict(self.env,RESTIC_PASSWORD='wrong-password')
        out=Runner(Config.from_env(e)).restore();self.assertEqual(out['status'],'skipped')
        self.assertEqual((c.data_path/'keep').read_bytes(),b'existing')

    def test_wrong_password_never_initializes_repository(self):
        (self.root/'data'/'fixture').write_bytes(b'existing')
        before=(self.root/'repo'/'config').read_bytes()
        with self.assertRaises(BackupError):Runner(Config.from_env(dict(self.env,RESTIC_PASSWORD='wrong'))).backup()
        self.assertEqual(before,(self.root/'repo'/'config').read_bytes())

    def test_shared_repo_different_dataset_is_not_selected(self):
        c=Config.from_env(self.env);(c.data_path/'fixture').write_bytes(b'a');Runner(c).backup()
        dest=self.root/'empty';dest.mkdir()
        e=dict(self.env,DATA_PATH=str(dest),BACKUP_WORK_PATH=str(self.root/'work-b'),DATASET_ID='other',REPOSITORY_ISOLATED='false')
        with self.assertRaises(BackupError) as exc:Runner(Config.from_env(e)).restore()
        self.assertEqual(exc.exception.code,'no_restore_candidate')
        self.assertEqual(list(dest.iterdir()),[])
