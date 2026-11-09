"""Synthetic configuration, latest selection, failure and window regression tests."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock
from zoneinfo import ZoneInfo
import json
import os
import sys
import time
from k3s_backup.config import Config
from k3s_backup.errors import BackupError
from k3s_backup.manifest import create, verify
from k3s_backup.process import run, Result
from k3s_backup.repository import Repository
from k3s_backup.runner import Runner
from k3s_backup.scheduler import Cron, in_window


def config(root, **extra):
    return Config.from_env({"RESTIC_REPOSITORY": str(root / "repo"), "RESTIC_PASSWORD": "synthetic", "DATA_PATH": str(root / "data"), "BACKUP_WORK_PATH": str(root / "work"), "BACKUP_MIN_FREE_BYTES": "1", **extra})


class ConfigTests(TestCase):
    def test_pod_node_identity_not_used(self):
        with TemporaryDirectory() as t:
            r = Path(t)
            a = config(r, HOSTNAME="old-pod", NODE_NAME="old-node")
            b = config(r, HOSTNAME="new-pod", NODE_NAME="new-node")
            self.assertEqual(a.dataset_id, b.dataset_id)
            self.assertNotIn("synthetic", repr(a))

    def test_shared_repo_requires_dataset(self):
        with TemporaryDirectory() as t:
            with self.assertRaises(BackupError): config(Path(t), REPOSITORY_ISOLATED="false")

    def test_secret_file_and_aliases(self):
        with TemporaryDirectory() as t:
            r = Path(t); p = r / "password"; p.write_text("test-pass\n")
            c = config(r, DATABASE_TYPE="pg", POSTGRES_USER="test-user", POSTGRES_DB="fixture", DATABASE_PASSWORD_FILE=str(p))
            self.assertEqual((c.engine, c.db_user, c.databases, c.db_password), ("postgres", "test-user", ["fixture"], "test-pass"))
            with self.assertRaises(BackupError): config(r, DATABASE_PASSWORD_FILE=str(p), DATABASE_PASSWORD="conflicting", DATABASE_TYPE="pg", POSTGRES_USER="u", POSTGRES_DB="d")

    def test_rest_endpoint_password_rotation_does_not_change_dataset(self):
        with TemporaryDirectory() as t:
            a=config(Path(t), RESTIC_REPOSITORY="rest:https://alice:first@fixture.invalid/repo")
            b=config(Path(t), RESTIC_REPOSITORY="rest:https://alice:second@fixture.invalid/repo")
            self.assertEqual(a.dataset_id,b.dataset_id)

    def test_mariadb_alias_host_and_port(self):
        with TemporaryDirectory() as t:
            c = config(Path(t), DATABASE_TYPE="mariadb", MARIADB_HOST="fixture-db", MARIADB_PORT="3307", MARIADB_USER="user", MARIADB_DATABASE="fixture")
            self.assertEqual((c.db_host, c.db_port), ("fixture-db", "3307"))

    def test_dual_backup_mode_supported(self):
        with TemporaryDirectory() as t:
            c = config(Path(t), DATABASE_TYPE="pg", DATABASE_USER="u", DATABASE_NAME="db", BACKUP_MODE="dual")
            self.assertEqual(c.env["BACKUP_MODE"], "dual")

    def test_unsafe_overlap_rejected(self):
        with TemporaryDirectory() as t:
            with self.assertRaises(BackupError): config(Path(t), BACKUP_WORK_PATH=t + "/data/backup")


class ScheduleTests(TestCase):
    def test_actual_project_crons(self):
        now = datetime(2026, 11, 9, 18, 13, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertTrue(Cron("13 */1 * * *").matches(now))
        self.assertFalse(Cron("0 */2 * * *").matches(now))
        self.assertTrue(in_window("14-23", now))
        self.assertFalse(in_window("0-5", now))
        self.assertTrue(in_window("22-3", now.replace(hour=1)))

    def test_dom_star_step_does_not_turn_weekday_into_or(self):
        self.assertFalse(Cron("0 0 */1 * 1").matches(datetime(2026, 11, 10, 0, 0)))

    def test_invalid_cron(self):
        for c in ["0 0 * * *;touch /bad", "*/0 * * * *", "60 * * * *"]:
            with self.assertRaises(BackupError): Cron(c)

    def test_window_skip_does_not_probe_or_publish(self):
        with TemporaryDirectory() as t:
            c = config(Path(t), DATABASE_TYPE="pg", DATABASE_USER="u", DATABASE_NAME="d", DATABASE_BACKUP_TIME="0-5")
            repo = mock.Mock(); r = Runner(c, repo)
            result = r.backup(datetime(2026, 11, 9, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
            self.assertEqual(result["status"], "skipped_window")
            repo.probe.assert_not_called(); repo.backup.assert_not_called()
            self.assertFalse((c.work_path / "status/last_success.json").exists())


class ManifestTests(TestCase):
    def test_complete_and_modified_artifact(self):
        with TemporaryDirectory() as t:
            r = Path(t); stage = r / "stage"; stage.mkdir(); (stage / "fixture").write_bytes(b"hello")
            c = config(r); create(stage, c, {"engine": "files"}); verify(stage, c)
            (stage / "fixture").write_bytes(b"bad")
            with self.assertRaises(BackupError): verify(stage, c)

    def test_escape_symlink(self):
        with TemporaryDirectory() as t:
            r = Path(t); stage = r / "stage"; stage.mkdir(); (stage / "evil").symlink_to("/etc/passwd")
            with self.assertRaises(BackupError): create(stage, config(r), {})

    def test_dataset_not_interchangeable(self):
        with TemporaryDirectory() as t:
            r = Path(t); stage = r / "stage"; stage.mkdir(); (stage / "fixture").write_bytes(b"hello")
            create(stage, config(r, DATASET_ID="one"), {})
            with self.assertRaises(BackupError): verify(stage, config(r, DATASET_ID="two"))


class RepositoryTests(TestCase):
    def test_latest_is_chronological_not_physical_first(self):
        with TemporaryDirectory() as t:
            c = config(Path(t), DATABASE_TYPE="pg", DATABASE_USER="u", DATABASE_NAME="db", RESTORE_LEGACY_PHYSICAL="true")
            repo = Repository(c); now = datetime.now(timezone.utc)
            def snap(i, when, tags, paths):return {"id":str(i)*64,"time":when.isoformat(),"hostname":"random-"+str(i),"tags":tags,"paths":paths}
            repo.snapshots = lambda: [snap(1, now-timedelta(hours=5), [], ["/data"]), snap(2, now, ["schema=1","dataset="+c.dataset_id,"kind=pg-logical"], ["/backup/current"]), snap(3, now, ["schema=1","dataset=foreign","kind=pg-logical"], ["/backup/current"])]
            candidates = repo.candidates()
            self.assertEqual([x['id'] for x in candidates], ["2"*64,"1"*64])

    def test_probe_failure_never_initializes(self):
        with TemporaryDirectory() as t:
            r = Repository(config(Path(t)))
            with mock.patch('k3s_backup.repository.run', return_value=Result(1,b'',b'wrong password')) as p:
                with self.assertRaises(BackupError): r.probe()
                self.assertNotIn('init', p.call_args[0][0]); self.assertNotIn('unlock', p.call_args[0][0])

    def test_broken_pack_never_repairs_or_prunes(self):
        with TemporaryDirectory() as t:
            c = config(Path(t), AUTO_REPAIR_INDEX="true", MAINTENANCE_EXCLUSIVE="true", REPAIR_ROLLBACK_AVAILABLE="true")
            r = Repository(c)
            out = json.dumps({"message_type":"summary","broken_packs":["bad"],"suggest_repair_index":True}).encode()
            with mock.patch.object(r,'command',return_value=Result(1,out,b'')) as p:
                with self.assertRaises(BackupError):r.check()
                self.assertEqual(p.call_count,1)

    def test_prune_apply_observation_gate(self):
        with TemporaryDirectory() as t:
            r = Repository(config(Path(t), RESTIC_FORGET_ARGS="--keep-last 7"))
            with self.assertRaises(BackupError):r.prune(apply=True)

    def test_file_excludes_are_not_shell(self):
        with TemporaryDirectory() as t:
            r = Repository(config(Path(t), RESTIC_JOB_ARGS="--exclude cache --one-file-system"))
            self.assertEqual(r.file_args(), ["--exclude", "cache", "--one-file-system"])
            r = Repository(config(Path(t), RESTIC_JOB_ARGS="--password-command steal"))
            with self.assertRaises(BackupError): r.file_args()

    def test_mixed_native_physical_logical_ignores_host(self):
        with TemporaryDirectory() as t:
            c = config(Path(t), DATABASE_TYPE="pg", DATABASE_USER="u", DATABASE_NAME="db")
            r = Repository(c); now = datetime.now(timezone.utc)
            r.snapshots = lambda: [{"id": "1"*64, "time": (now-timedelta(hours=1)).isoformat(), "paths": ["/backup/current"], "tags": ["schema=1", "dataset="+c.dataset_id,"kind=pg-physical"], "hostname":"gone-node"}, {"id":"2"*64,"time":now.isoformat(),"paths":["/backup/current"],"tags":["schema=1","dataset="+c.dataset_id,"kind=pg-logical"],"hostname":"new-name"}]
            self.assertEqual([x["id"] for x in r.candidates()], ["2"*64,"1"*64])

    def test_restore_preflight_rejects_total_size_before_restore(self):
        with TemporaryDirectory() as t:
            c=config(Path(t), BACKUP_MAX_BYTES="100")
            r=Repository(c)
            with mock.patch.object(r,"command",return_value=Result(0,b'{"total_size":101}',b"")) as p:
                with self.assertRaises(BackupError) as e:r.restore({"id":"a"*64},Path(t))
                self.assertEqual(e.exception.code,"size_limit")
                self.assertEqual(p.call_count,1)

    def test_whole_repo_check_has_no_host_or_path(self):
        with TemporaryDirectory() as t:
            r = Repository(config(Path(t)))
            with mock.patch.object(r,'command',return_value=Result(0,b'',b'')) as p:
                self.assertEqual(r.check()['scope'],'whole_repository')
                self.assertEqual(p.call_args[0][0],['check','--json'])


class ProcessTests(TestCase):
    def test_nonzero_never_leaks_stderr(self):
        with self.assertRaises(BackupError) as e:run([sys.executable,'-c','import sys; print("do-not-leak"); sys.exit(2)'])
        self.assertNotIn('do-not-leak',str(e.exception))

    def test_timeout(self):
        t=time.monotonic()
        with self.assertRaises(BackupError) as e:run([sys.executable,'-c','import time;time.sleep(30)'],timeout=1)
        self.assertEqual(e.exception.code,'command_timeout'); self.assertLess(time.monotonic()-t,8)
