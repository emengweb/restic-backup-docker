"""Real restic 0.16 -> 0.19 shadow repository compatibility, synthetic only."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase,skipUnless
import json
import os
import subprocess
from k3s_backup.config import Config
from k3s_backup.runner import Runner

OLD=os.environ.get('RESTIC_LEGACY_BIN')
NEW=os.environ.get('RESTIC_BIN')

@skipUnless(OLD and NEW,'NOT RUN: legacy and current restic tools are required')
class LegacyRepositoryTests(TestCase):
    def test_old_snapshots_readable_after_new_backup_without_repo_upgrade(self):
        with TemporaryDirectory() as t:
            r=Path(t);(r/'data').mkdir();(r/'data'/'fixture').write_bytes(b'old-payload')
            env=dict(os.environ,RESTIC_REPOSITORY=str(r/'repo'),RESTIC_PASSWORD='synthetic-only')
            def call(binary,*args):
                p=subprocess.run([binary,'--no-cache',*args],env=env,capture_output=True)
                self.assertEqual(p.returncode,0,'synthetic restic compatibility command failed')
                return p.stdout
            call(OLD,'init');call(OLD,'backup',str(r/'data'))
            old=json.loads(call(OLD,'snapshots','--json'))[-1]
            before=(r/'repo'/'config').read_bytes()
            call(NEW,'check','--read-data')
            call(NEW,'restore',old['id'],'--target',str(r/'old-restore'),'--verify')
            oldpath=(r/'old-restore').joinpath(*(r/'data').parts[1:])/'fixture'
            self.assertEqual(oldpath.read_bytes(),b'old-payload')
            c=Config.from_env(dict(env,DATASET_ID='fixture',DATA_PATH=str(r/'data'),BACKUP_WORK_PATH=str(r/'work'),RESTIC_BIN=NEW,BACKUP_MIN_FREE_BYTES='1'))
            (r/'data'/'fixture').write_bytes(b'new-payload');Runner(c).backup()
            # New client did not silently change config/repository format.
            self.assertEqual(before,(r/'repo'/'config').read_bytes())
            self.assertEqual(len(json.loads(call(OLD,'snapshots','--json'))),2)
            call(OLD,'check','--read-data')
