"""Synthetic budget tests, without any production filesystem or server."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import os
from k3s_backup.config import Config
from k3s_backup.runner import Runner
from k3s_backup.limits import capacity_budget, poll_capacity
from k3s_backup.errors import BackupError

class ResourceGuards(TestCase):
    def test_aggregate_not_only_single_file(self):
        with TemporaryDirectory() as t:
            d=Path(t);(d/'a').write_bytes(b'x'*60);(d/'b').write_bytes(b'x'*60)
            with self.assertRaises(BackupError) as e:
                with capacity_budget([d],100,1):pass
            self.assertEqual(e.exception.code,'size_limit')
    def test_capacity_context_does_not_leak_into_next_job(self):
        with TemporaryDirectory() as t:
            with capacity_budget([Path(t)],100,1):poll_capacity()
            poll_capacity()
    def test_nonempty_start_does_not_need_restore_payload_headroom(self):
        with TemporaryDirectory() as t:
            root=Path(t);(root/'data').mkdir();(root/'data'/'keep').write_text('preserve')
            c=Config.from_env({'RESTIC_REPOSITORY':str(root/'repo'),'DATA_PATH':str(root/'data'),'BACKUP_WORK_PATH':str(root/'work')})
            r=Runner(c)
            with patch.object(r,'headroom',side_effect=AssertionError('must not check payload headroom')):
                self.assertEqual(r.restore()['status'],'skipped')
