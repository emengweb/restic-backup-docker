"""Regress latest fallback/resume across marker state using synthetic repos."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from datetime import datetime, timedelta, timezone
import shutil
from k3s_backup.config import Config
from k3s_backup.runner import Runner
from k3s_backup import manifest
from k3s_backup.errors import BackupError


class SyntheticRepo:
    def __init__(self,c,root):
        self.c=c;self.root=root;self.roots={};self.items=[];self.probes=0
    def add(self,ident,payload,when,corrupt=False):
        d=self.root/ident;d.mkdir();(d/'payload').mkdir();(d/'payload'/'fixture').write_bytes(payload)
        manifest.create(d,self.c,{'engine':'files'})
        if corrupt:(d/'payload'/'fixture').write_bytes(b'corrupt')
        s={'id':ident*64,'time':when.isoformat(),'paths':['/envelope'],'_mode':'manifest'}
        self.roots[s['id']]=d;self.items.append(s)
    def probe(self):self.probes+=1
    def candidates(self,resume_id=None):return sorted(self.items,key=lambda s:s['time'],reverse=True)
    def restore(self,s,target):shutil.copytree(self.roots[s['id']],target/'envelope')
    def envelope(self,s,target):return target/'envelope'


class RecoveryIntegration(TestCase):
    def test_corrupt_newest_resets_only_owned_stage_then_restores_previous(self):
        with TemporaryDirectory() as t:
            r=Path(t);(r/'data').mkdir();repo_root=r/'snapshots';repo_root.mkdir()
            c=Config.from_env({'RESTIC_REPOSITORY':str(r/'repo'),'DATA_PATH':str(r/'data'),'BACKUP_WORK_PATH':str(r/'work'),'DATASET_ID':'fixture','BACKUP_MIN_FREE_BYTES':'1'})
            repo=SyntheticRepo(c,repo_root);now=datetime.now(timezone.utc)
            repo.add('a',b'good previous',now-timedelta(minutes=30))
            repo.add('b',b'newest',now,corrupt=True)
            result=Runner(c,repo).restore()
            self.assertEqual(result['status'],'restored');self.assertEqual(result['fallback_count'],1)
            self.assertEqual((r/'data'/'payload'/'fixture').read_bytes() if (r/'data'/'payload').exists() else (r/'data'/'fixture').read_bytes(),b'good previous')
    def test_completed_restore_skips_offline_repository(self):
        with TemporaryDirectory() as t:
            r=Path(t);(r/'data').mkdir();(r/'snapshots').mkdir()
            c=Config.from_env({'RESTIC_REPOSITORY':str(r/'repo'),'DATA_PATH':str(r/'data'),'BACKUP_WORK_PATH':str(r/'work'),'DATASET_ID':'fixture','BACKUP_MIN_FREE_BYTES':'1'})
            repo=SyntheticRepo(c,r/'snapshots');repo.add('a',b'good',datetime.now(timezone.utc))
            runner=Runner(c,repo);runner.restore()
            repo.probe=lambda:(_ for _ in ()).throw(AssertionError('repository must not be contacted'))
            again=runner.restore();self.assertEqual(again['status'],'skipped')
