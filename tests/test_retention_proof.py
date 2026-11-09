"""Destructive whole-repo retention requires persisted matching proof, not env age."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime, timedelta, timezone
from unittest import TestCase, mock
from k3s_backup.config import Config
from k3s_backup.repository import Repository
from k3s_backup.process import Result
from k3s_backup.errors import BackupError

class RetentionProofTests(TestCase):
    def test_booleans_are_not_enough(self):
        with TemporaryDirectory() as t:
            r=Path(t)
            c=Config.from_env({'RESTIC_REPOSITORY':str(r/'repo'),'DATA_PATH':str(r/'data'),'BACKUP_WORK_PATH':str(r/'work'),'RESTIC_FORGET_ARGS':'--keep-last 7','MAINTENANCE_EXCLUSIVE':'true','RETENTION_APPLY':'true','VERIFIED_STABLE_SINCE':'2020-01-01T00:00:00Z','RECENT_RESTORE_VERIFIED':'true'})
            with self.assertRaises(BackupError) as e:Repository(c).prune(apply=True)
            self.assertEqual(e.exception.code,'observation_gate')
    def test_matching_recent_persisted_proof_and_keep7d(self):
        with TemporaryDirectory() as t:
            r=Path(t);now=datetime.now(timezone.utc);proof=r/'proof.json'
            c=Config.from_env({'RESTIC_REPOSITORY':str(r/'repo'),'DATA_PATH':str(r/'data'),'BACKUP_WORK_PATH':str(r/'work'),'DATASET_ID':'fixture','RESTIC_FORGET_ARGS':'--prune --keep-last 7','MAINTENANCE_EXCLUSIVE':'true','RETENTION_APPLY':'true','RETENTION_PROOF_FILE':str(proof)})
            sid='a'*64; snaps=[{'id':sid,'time':now.isoformat(),'paths':['/backup/current'],'tags':['dataset=fixture','kind=pg-logical']}]
            proof.write_text(json.dumps({'schema_version':1,'repository_identity':'fixture','stable_since':(now-timedelta(days=8)).isoformat(),'observed_until':(now-timedelta(minutes=3)).isoformat(),'unresolved_failures':0,'restore_verified':[{'snapshot_id':sid,'verified_at':(now-timedelta(minutes=5)).isoformat()}]}))
            repo=Repository(c)
            with mock.patch.object(repo,'snapshots',return_value=snaps),mock.patch.object(repo,'check',return_value={'status':'checked'}),mock.patch.object(repo,'command',return_value=Result(0)) as cmd:
                self.assertEqual(repo.prune(apply=True)['status'],'pruned')
                forget=cmd.call_args_list[0][0][0]
                self.assertIn('--keep-within',forget);self.assertIn('7d',forget)
                self.assertEqual(cmd.call_args_list[1][0][0],['prune'])
    def test_keep_zero_rejected(self):
        with TemporaryDirectory() as t:
            r=Path(t);c=Config.from_env({'RESTIC_REPOSITORY':str(r/'repo'),'DATA_PATH':str(r/'data'),'BACKUP_WORK_PATH':str(r/'work'),'RESTIC_FORGET_ARGS':'--keep-last 0'})
            with self.assertRaises(BackupError):Repository(c).prune()
