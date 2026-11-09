"""Synthetic filesystem tests only: no repository, DB, credentials or mounts.

Mount boundaries are injected from proc mount IDs; no host mount operation is
performed. Crash tests deliberately leave only disposable TemporaryDirectory
fixtures, then exercise the same public API a restarted container would call.
"""

import ctypes
import errno
import fcntl
import json
import multiprocessing
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from k3s_backup import restore
from k3s_backup.errors import BackupError


class Crash(BaseException):
    pass


def _hard_crash_population(root):
    config = SimpleNamespace(data_path=Path(root), dataset_id="synthetic", env={})

    def populate(stage):
        (stage / "partial").write_bytes(b"synthetic partial data")
        os._exit(87)

    restore.restore_to_volume(config, "a" * 64, populate)


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="k3s-restore-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "volume"
        self.root.mkdir()
        self.config = SimpleNamespace(data_path=self.root, work_path=self.base / "unused-work",
                                      dataset_id="synthetic", env={})
        self.snapshot = "a" * 64

    def run_restore(self, populate=None, snapshot=None, config=None):
        return restore.restore_to_volume(config or self.config, snapshot or self.snapshot,
                                         populate or self.populate)

    @staticmethod
    def populate(stage):
        (stage / "a").write_bytes(b"synthetic-alpha")
        (stage / "b").mkdir()
        (stage / "b" / "nested").write_bytes(b"synthetic-beta")
        (stage / "b" / "relative").symlink_to("../a")

    def marker(self):
        return json.loads((self.root / restore.MARKER).read_text())

    def stage(self):
        return self.root / self.marker()["stage"]

    def rewrite_marker(self, mutate):
        marker = self.marker()
        mutate(marker)
        (self.root / restore.MARKER).write_text(json.dumps(marker))

    def error(self, code, callback):
        with self.assertRaises(BackupError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn(str(self.base), str(raised.exception))
        self.assertNotIn("synthetic-private-error", str(raised.exception))
        return raised.exception

    def interrupted_population(self):
        def populate(stage):
            (stage / "partial").write_bytes(b"synthetic partial")
            raise Crash()
        with self.assertRaises(Crash):
            self.run_restore(populate)
        self.assertEqual(self.marker()["phase"], "populating")
        return self.stage() / "payload"

    def interrupted_publish(self, *, after_move=False):
        original = restore._rename_noreplace

        def crash(src, name, dst):
            if after_move:
                original(src, name, dst)
            raise Crash()

        with mock.patch.object(restore, "_rename_noreplace", side_effect=crash):
            with self.assertRaises(Crash):
                self.run_restore()
        self.assertEqual(self.marker()["phase"], "publishing")
        return self.stage() / "payload"

    def assert_restored(self):
        self.assertEqual((self.root / "a").read_bytes(), b"synthetic-alpha")
        self.assertEqual((self.root / "b" / "nested").read_bytes(), b"synthetic-beta")
        self.assertTrue((self.root / "b" / "relative").is_symlink())
        self.assertEqual(self.marker()["phase"], "complete")
        self.assertEqual(list(self.root.glob(".k3s-restic-stage-*")), [])

    def test_basic_same_volume_root_inode_and_permissions(self):
        before = self.root.stat()

        def populate(stage):
            self.assertEqual(stage.stat().st_dev, self.root.stat().st_dev)
            self.assertEqual(list(stage.iterdir()), [])
            self.assertEqual(stage.parent.parent, self.root)
            self.assertEqual(stat.S_IMODE(stage.parent.stat().st_mode), 0o700)
            self.populate(stage)
            stage.chmod(0o750)
            (stage / "a").chmod(0o640)

        self.assertEqual(self.run_restore(populate), {"status": "restored"})
        self.assert_restored()
        self.assertEqual(self.root.stat().st_ino, before.st_ino)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o750)
        self.assertEqual(stat.S_IMODE((self.root / "a").stat().st_mode), 0o640)
        self.assertFalse(self.config.work_path.exists())
        self.assertEqual(stat.S_IMODE((self.root / restore.MARKER).stat().st_mode), 0o600)

    def test_completed_marker_does_not_trigger_restore_again(self):
        self.run_restore()
        # Application changes AFTER successful restore are never overwritten.
        (self.root / "a").write_bytes(b"application synthetic mutation")
        callback = mock.Mock(side_effect=AssertionError("must not populate"))
        self.assertEqual(self.run_restore(callback), {"status": "skipped", "reason": "already_restored"})
        self.assertEqual(self.run_restore(callback, snapshot="b" * 64),
                         {"status": "skipped", "reason": "nonempty"})
        callback.assert_not_called()
        self.assertEqual((self.root / "a").read_bytes(), b"application synthetic mutation")

    def test_nonempty_and_forcesync_never_delete_unrelated_data(self):
        for policy in ({}, {"FORCESYNC": "true"}, {"FORCESYNC": "1"}):
            with self.subTest(policy=policy):
                self.config.env = policy
                (self.root / "existing").write_bytes(b"preserve")
                callback = mock.Mock()
                self.assertEqual(self.run_restore(callback), {"status": "skipped", "reason": "nonempty"})
                callback.assert_not_called()
                self.assertEqual((self.root / "existing").read_bytes(), b"preserve")
                self.assertFalse((self.root / restore.MARKER).exists())

    def test_nonempty_dangling_symlink_is_application_data(self):
        (self.root / "existing").symlink_to("missing")
        self.assertEqual(self.run_restore(), {"status": "skipped", "reason": "nonempty"})
        self.assertTrue((self.root / "existing").is_symlink())

    def test_empty_payload_is_completed_and_metadata_not_app_data(self):
        self.assertEqual(self.run_restore(lambda _: None), {"status": "restored"})
        self.assertEqual(self.marker()["owned_entries"], [])
        self.assertEqual(self.run_restore(), {"status": "skipped", "reason": "already_restored"})

    def test_population_exception_is_safe_and_retry_cleans_only_private_stage(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "sentinel").write_bytes(b"preserve")

        def fail(stage):
            (stage / "partial").write_bytes(b"partial")
            (stage / "outside-link").symlink_to(outside, target_is_directory=True)
            raise ValueError("synthetic-private-error")

        self.error("restore_populate_failed", lambda: self.run_restore(fail))
        self.assertFalse((self.root / "partial").exists())
        self.assertEqual(self.marker()["phase"], "populating")

        def retry(stage):
            self.assertEqual(list(stage.iterdir()), [])
            self.populate(stage)

        self.assertEqual(self.run_restore(retry), {"status": "restored"})
        self.assertEqual((outside / "sentinel").read_bytes(), b"preserve")
        self.assert_restored()

    def test_actual_process_death_population_resumes(self):
        process = multiprocessing.get_context("fork").Process(
            target=_hard_crash_population, args=(str(self.root),))
        process.start()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join()
            self.fail("synthetic crash child did not exit")
        self.assertEqual(process.exitcode, 87)
        self.assertEqual(self.marker()["phase"], "populating")
        self.assertEqual(self.run_restore(), {"status": "restored"})
        self.assert_restored()

    def test_resume_before_atomic_move(self):
        self.interrupted_publish()
        callback = mock.Mock(side_effect=AssertionError("must not repopulate"))
        self.assertEqual(self.run_restore(callback), {"status": "restored"})
        callback.assert_not_called()
        self.assert_restored()

    def test_resume_after_atomic_move_before_journal_update(self):
        payload = self.interrupted_publish(after_move=True)
        self.assertTrue((self.root / "a").exists())
        self.assertFalse((payload / "a").exists())
        self.assertFalse(self.marker()["owned_entries"][0]["published"])
        callback = mock.Mock(side_effect=AssertionError("must not repopulate"))
        self.assertEqual(self.run_restore(callback), {"status": "restored"})
        callback.assert_not_called()
        self.assert_restored()

    def test_resume_after_one_durable_published_entry(self):
        save = restore._Journal.save

        def crash(journal, stage):
            save(journal, stage)
            if journal.data["phase"] == "publishing" and journal.data["owned_entries"][0]["published"]:
                raise Crash()

        with mock.patch.object(restore._Journal, "save", crash):
            with self.assertRaises(Crash):
                self.run_restore()
        self.assertTrue(self.marker()["owned_entries"][0]["published"])
        self.assertEqual(self.run_restore(), {"status": "restored"})
        self.assert_restored()

    def test_crash_during_each_allocation_journal_transition(self):
        save = restore._Journal.save
        for transition in (1, 2, 3):
            with self.subTest(transition=transition):
                root = self.base / f"alloc-{transition}"
                root.mkdir()
                config = SimpleNamespace(data_path=root, dataset_id="synthetic", env={})
                calls = [0]

                def crash(journal, stage):
                    save(journal, stage)
                    calls[0] += 1
                    if calls[0] == transition:
                        raise Crash()

                with mock.patch.object(restore._Journal, "save", crash):
                    with self.assertRaises(Crash):
                        self.run_restore(config=config)
                self.assertEqual(self.run_restore(config=config), {"status": "restored"})
                self.assertEqual((root / "a").read_bytes(), b"synthetic-alpha")

    def test_crash_after_mkdir_before_identity_journal_is_recoverable(self):
        mkdir = restore.os.mkdir
        for name in ("stage", "payload"):
            with self.subTest(name=name):
                root = self.base / f"mkdir-{name}"
                root.mkdir()
                config = SimpleNamespace(data_path=root, dataset_id="synthetic", env={})

                def crash(path, *args, **kwargs):
                    mkdir(path, *args, **kwargs)
                    if ((name == "stage" and str(path).startswith(restore._STAGE_PREFIX))
                            or (name == "payload" and path == "payload")):
                        raise Crash()

                with mock.patch.object(restore.os, "mkdir", side_effect=crash):
                    with self.assertRaises(Crash):
                        self.run_restore(config=config)
                self.assertEqual(self.run_restore(config=config), {"status": "restored"})

    def test_complete_marker_cleanup_interruption_retries_without_populate(self):
        with mock.patch.object(restore, "_cleanup_complete", side_effect=Crash()):
            with self.assertRaises(Crash):
                self.run_restore()
        self.assertEqual(self.marker()["phase"], "complete")
        callback = mock.Mock(side_effect=AssertionError("must not populate"))
        self.assertEqual(self.run_restore(callback), {"status": "skipped", "reason": "already_restored"})
        callback.assert_not_called()
        self.assert_restored()

    def test_cleanup_after_payload_rmdir_is_recoverable(self):
        rmdir = restore.os.rmdir

        def crash(name, *args, **kwargs):
            rmdir(name, *args, **kwargs)
            if name == "payload":
                raise Crash()

        with mock.patch.object(restore.os, "rmdir", side_effect=crash):
            with self.assertRaises(Crash):
                self.run_restore()
        self.assertEqual(self.run_restore(), {"status": "skipped", "reason": "already_restored"})
        self.assert_restored()

    def test_wrong_dataset_or_snapshot_cannot_reclaim_stage(self):
        payload = self.interrupted_population()
        for field, value in (("dataset_id", "other"), ("snapshot", "b" * 64)):
            with self.subTest(field=field):
                config = SimpleNamespace(data_path=self.root, dataset_id="synthetic", env={})
                if field == "dataset_id":
                    config.dataset_id = value
                self.error("restore_conflict", lambda: self.run_restore(
                    config=config, snapshot=value if field == "snapshot" else None))
                self.assertEqual((payload / "partial").read_bytes(), b"synthetic partial")

    def test_unrelated_root_entry_after_population_interrupt_is_never_deleted(self):
        self.interrupted_population()
        (self.root / "unrelated").write_bytes(b"preserve")
        self.config.env["FORCESYNC"] = "true"
        self.error("restore_conflict", self.run_restore)
        self.assertEqual((self.root / "unrelated").read_bytes(), b"preserve")

    def test_destination_collision_after_preflight_is_not_overwritten(self):
        original = restore._rename_noreplace

        def concurrent_writer(src, name, dst):
            (self.root / name).write_bytes(b"concurrent writer")
            original(src, name, dst)

        with mock.patch.object(restore, "_rename_noreplace", side_effect=concurrent_writer):
            self.error("restore_conflict", self.run_restore)
        self.assertEqual((self.root / "a").read_bytes(), b"concurrent writer")
        self.assertEqual((self.stage() / "payload" / "a").read_bytes(), b"synthetic-alpha")
        self.error("restore_conflict", self.run_restore)

    def test_same_named_directory_collision_preserves_both(self):
        payload = self.interrupted_publish()
        (self.root / "b").mkdir()
        (self.root / "b" / "unrelated").write_bytes(b"preserve")
        self.error("restore_conflict", self.run_restore)
        self.assertTrue((payload / "b" / "nested").exists())
        self.assertEqual((self.root / "b" / "unrelated").read_bytes(), b"preserve")

    def test_missing_owned_entry_cannot_be_marked_complete(self):
        payload = self.interrupted_publish()
        (payload / "a").unlink()
        self.error("restore_ownership", self.run_restore)
        self.assertEqual(self.marker()["phase"], "publishing")

    def test_replaced_published_inode_is_rejected(self):
        self.interrupted_publish(after_move=True)
        original = self.root / "a"
        saved = self.base / "saved-a"
        original.rename(saved)
        original.write_bytes(b"replacement")
        self.error("restore_ownership", self.run_restore)
        self.assertEqual(original.read_bytes(), b"replacement")
        self.assertEqual(saved.read_bytes(), b"synthetic-alpha")

    def test_target_root_and_symlink_components_are_rejected(self):
        alias = self.base / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        ancestor = self.base / "ancestor"
        ancestor.symlink_to(self.base, target_is_directory=True)
        for path in (Path("/"), Path("//"), Path("relative"), self.base / ".." / self.base.name / "volume",
                     alias, ancestor / "volume"):
            with self.subTest(path=str(path)):
                config = SimpleNamespace(data_path=path, dataset_id="synthetic", env={})
                self.error("restore_path", lambda: self.run_restore(config=config))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_target_replacement_during_callback_is_detected(self):
        old = self.base / "original-volume"

        def replace(stage):
            (stage / "a").write_bytes(b"staged")
            self.root.rename(old)
            self.root.mkdir()
            (self.root / "unrelated").write_bytes(b"preserve")

        self.error("restore_path", lambda: self.run_restore(replace))
        self.assertEqual((self.root / "unrelated").read_bytes(), b"preserve")
        self.assertFalse((self.root / "a").exists())
        self.assertTrue((old / restore.MARKER).exists())

    def test_marker_symlink_fifo_and_hardlink_are_rejected_without_follow(self):
        marker = self.root / restore.MARKER
        outside = self.base / "outside"
        outside.write_bytes(b"preserve")
        for kind in ("symlink", "fifo", "hardlink"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    marker.symlink_to(outside)
                elif kind == "fifo":
                    os.mkfifo(marker, 0o600)
                else:
                    os.link(outside, marker)
                self.error("restore_marker", self.run_restore)
                self.assertEqual(outside.read_bytes(), b"preserve")
                marker.unlink()

    def test_marker_invalid_json_duplicate_fields_and_world_writable_rejected(self):
        marker = self.root / restore.MARKER
        for raw in (b"", b"{", b"[]", b'{"a":1,"a":2}', b'null', b'\xff'):
            with self.subTest(raw=raw):
                marker.write_bytes(raw)
                marker.chmod(0o600)
                self.error("restore_marker", self.run_restore)
                marker.unlink()
        self.interrupted_population()
        marker.chmod(0o666)
        self.error("restore_marker", self.run_restore)

    def test_marker_owner_mismatch_rejected(self):
        if os.geteuid() != 0:
            self.skipTest("changing ownership requires root")
        self.interrupted_population()
        os.chown(self.root / restore.MARKER, 65534, 65534)
        self.error("restore_marker", self.run_restore)

    def test_marker_path_and_state_tampering_are_rejected(self):
        self.interrupted_population()
        original = self.marker()
        mutations = (
            lambda data: data.update(stage="../outside"),
            lambda data: data.update(stage="/outside"),
            lambda data: data.update(owner_uid=os.geteuid() + 1),
            lambda data: data["target"].update(ino=data["target"]["ino"] + 1),
            lambda data: data.update(phase=[]),
            lambda data: data.update(stage_identity={"dev": 1, "ino": 2, "kind": []}),
            lambda data: data.update(owned_entries=[{"name": "../outside", "identity":
                                                    data["target"], "published": False}]),
            lambda data: data.update(root_metadata={"uid": 0, "gid": 0, "mode": 0o7777 + 1}),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                data = json.loads(json.dumps(original))
                mutation(data)
                (self.root / restore.MARKER).write_text(json.dumps(data))
                self.error("restore_marker", self.run_restore)
        (self.root / restore.MARKER).write_text(json.dumps(original))
        self.assertTrue((self.stage() / "payload" / "partial").exists())

    def test_stage_symlink_replacement_does_not_delete_external_tree(self):
        self.interrupted_population()
        stage = self.stage()
        stage.rename(self.base / "retained-stage")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "sentinel").write_bytes(b"preserve")
        stage.symlink_to(outside, target_is_directory=True)
        self.error("restore_ownership", self.run_restore)
        self.assertEqual((outside / "sentinel").read_bytes(), b"preserve")

    def test_stage_or_payload_inode_replacement_is_rejected(self):
        payload = self.interrupted_population()
        payload.rename(self.base / "saved-payload")
        payload.mkdir()
        (payload / "unrelated").write_bytes(b"preserve")
        self.error("restore_ownership", self.run_restore)
        self.assertEqual((payload / "unrelated").read_bytes(), b"preserve")

    def test_orphan_reserved_metadata_without_marker_requires_manual_attention(self):
        orphan = self.root / (restore._STAGE_PREFIX + "0" * 32)
        orphan.mkdir()
        (orphan / "sentinel").write_bytes(b"preserve")
        self.error("restore_marker", self.run_restore)
        self.assertEqual((orphan / "sentinel").read_bytes(), b"preserve")

    def test_partial_journal_temp_is_reclaimed_but_symlink_is_rejected(self):
        self.interrupted_population()
        temporary = self.stage() / restore._NEXT
        temporary.write_bytes(b"interrupted partial journal")
        temporary.chmod(0o600)
        self.assertEqual(self.run_restore(), {"status": "restored"})

    def test_untrusted_journal_temp_never_follows_link(self):
        self.interrupted_population()
        outside = self.base / "outside"
        outside.write_bytes(b"preserve")
        (self.stage() / restore._NEXT).symlink_to(outside)
        self.error("restore_marker", self.run_restore)
        self.assertEqual(outside.read_bytes(), b"preserve")

    def test_symlink_escape_absolute_reserved_and_cycles_rejected(self):
        for target in ("/tmp/outside", "../outside", "../../outside", restore.MARKER, "link"):
            with self.subTest(target=target):
                root = self.base / f"symlink-{len(list(self.base.iterdir()))}"
                root.mkdir()
                config = SimpleNamespace(data_path=root, dataset_id="synthetic", env={})
                self.error("restore_symlink", lambda: self.run_restore(
                    lambda stage: (stage / "link").symlink_to(target), config=config))
                self.assertFalse((root / "link").is_symlink())

    def test_symlink_lexical_parent_bypass_is_rejected(self):
        def populate(stage):
            (stage / "dir").mkdir()
            (stage / "dir" / "up").symlink_to("..")
            (stage / "link").symlink_to("dir/up/../outside")
        self.error("restore_symlink", lambda: self.run_restore(populate))
        self.assertFalse((self.root / "link").is_symlink())

    def test_symlink_relative_dangling_and_internal_chain_are_preserved(self):
        def populate(stage):
            self.populate(stage)
            (stage / "dangling").symlink_to("b/missing")
            (stage / "chain").symlink_to("b/relative")
        self.assertEqual(self.run_restore(populate), {"status": "restored"})
        self.assertEqual((self.root / "chain").read_bytes(), b"synthetic-alpha")
        self.assertTrue((self.root / "dangling").is_symlink())

    def test_reserved_payload_name_and_fifo_rejected_before_publication(self):
        for name in ("reserved", "fifo"):
            with self.subTest(name=name):
                root = self.base / f"unsafe-{name}"
                root.mkdir()
                config = SimpleNamespace(data_path=root, dataset_id="synthetic", env={})

                def populate(stage):
                    if name == "reserved":
                        (stage / restore.MARKER).write_bytes(b"untrusted")
                    else:
                        os.mkfifo(stage / "pipe")

                self.error("restore_unsafe_entry", lambda: self.run_restore(populate, config=config))
                self.assertFalse((root / "pipe").exists())

    def test_nested_mount_id_prevents_publish_even_on_same_device(self):
        original = restore._mount_id

        def mount(fd):
            if Path(os.readlink(f"/proc/self/fd/{fd}")).name == "nested-mount":
                return original(fd) + 1
            return original(fd)

        def populate(stage):
            (stage / "nested-mount").mkdir()
            (stage / "nested-mount" / "sentinel").write_bytes(b"preserve")

        with mock.patch.object(restore, "_mount_id", side_effect=mount):
            self.error("restore_mount_boundary", lambda: self.run_restore(populate))
        self.assertFalse((self.root / "nested-mount").exists())
        self.assertEqual((self.stage() / "payload" / "nested-mount" / "sentinel").read_bytes(), b"preserve")

    def test_nested_mount_prevents_any_cleanup_of_interrupted_payload(self):
        payload = self.interrupted_population()
        (payload / "nested-mount").mkdir()
        (payload / "nested-mount" / "sentinel").write_bytes(b"preserve")
        original = restore._mount_id

        def mount(fd):
            if Path(os.readlink(f"/proc/self/fd/{fd}")).name == "nested-mount":
                return original(fd) + 1
            return original(fd)

        with mock.patch.object(restore, "_mount_id", side_effect=mount):
            self.error("restore_mount_boundary", self.run_restore)
        self.assertTrue((payload / "partial").exists())
        self.assertEqual((payload / "nested-mount" / "sentinel").read_bytes(), b"preserve")

    def test_low_space_inodes_and_readonly_fail_before_callback(self):
        for changes, code in (({"f_bavail": 0}, "restore_no_space"),
                              ({"f_favail": 0}, "restore_no_space"),
                              ({"f_flag": os.ST_RDONLY}, "restore_readonly")):
            with self.subTest(changes=changes):
                info = SimpleNamespace(f_bavail=2**30, f_frsize=4096, f_favail=2**20, f_flag=0)
                for key, value in changes.items():
                    setattr(info, key, value)
                callback = mock.Mock()
                with mock.patch.object(restore.os, "fstatvfs", return_value=info):
                    self.error(code, lambda: self.run_restore(callback))
                callback.assert_not_called()
                self.assertEqual(list(self.root.iterdir()), [])

    def test_callback_enospc_is_diagnostic_and_partial_stage_can_retry(self):
        def fail(stage):
            (stage / "partial").write_bytes(b"partial")
            raise OSError(errno.ENOSPC, "synthetic-private-error")
        self.error("restore_no_space", lambda: self.run_restore(fail))
        self.assertEqual(self.marker()["phase"], "populating")
        self.assertEqual(self.run_restore(), {"status": "restored"})
        self.assert_restored()

    def test_low_space_after_population_never_claims_complete(self):
        original = restore.os.fstatvfs
        full = [False]

        def space(fd):
            if full[0]:
                return SimpleNamespace(f_bavail=0, f_frsize=4096, f_favail=100, f_flag=0)
            return original(fd)

        def populate(stage):
            self.populate(stage)
            full[0] = True

        with mock.patch.object(restore.os, "fstatvfs", side_effect=space):
            self.error("restore_no_space", lambda: self.run_restore(populate))
        self.assertEqual(self.marker()["phase"], "populating")
        self.assertFalse((self.root / "a").exists())
        self.assertEqual(self.run_restore(), {"status": "restored"})

    def test_reserve_configuration_is_validated(self):
        for value in ("bad", "0", "-1", "1023", None):
            with self.subTest(value=value):
                self.config.env = {"RESTORE_MIN_FREE_BYTES": value}
                self.error("restore_config", self.run_restore)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_directory_lock_prevents_overlapping_restore(self):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.error("restore_busy", self.run_restore)
        finally:
            os.close(fd)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_no_clobber_unsupported_fails_closed(self):
        with mock.patch.object(restore.ctypes, "CDLL", return_value=object()):
            self.error("restore_atomic_unsupported", self.run_restore)
        self.assertEqual(self.marker()["phase"], "publishing")
        self.assertFalse((self.root / "a").exists())
        self.assertEqual(self.run_restore(), {"status": "restored"})

    def test_fsync_files_directories_and_root_precede_complete_marker(self):
        fsync = restore.os.fsync
        save = restore._Journal.save
        observed = []

        def track(fd):
            info = os.fstat(fd)
            observed.append((info.st_dev, info.st_ino, stat.S_ISDIR(info.st_mode)))
            fsync(fd)

        def assert_durable(journal, stage):
            if journal.data["phase"] == "complete":
                expected = [self.root, self.root / "a", self.root / "b", self.root / "b" / "nested"]
                for path in expected:
                    info = path.stat()
                    self.assertIn((info.st_dev, info.st_ino, path.is_dir()), observed)
                self.assertTrue(all(entry["published"] for entry in journal.data["owned_entries"]))
            save(journal, stage)

        with mock.patch.object(restore.os, "fsync", side_effect=track):
            with mock.patch.object(restore._Journal, "save", assert_durable):
                self.assertEqual(self.run_restore(), {"status": "restored"})
        self.assert_restored()

    def test_fsync_error_after_move_is_not_success_and_can_resume(self):
        fsync = restore.os.fsync
        fail = [True]

        def error_after_move(fd):
            if fail[0] and (self.root / "a").exists():
                fail[0] = False
                raise OSError(errno.EIO, "synthetic-private-error")
            fsync(fd)

        with mock.patch.object(restore.os, "fsync", side_effect=error_after_move):
            self.error("restore_io", self.run_restore)
        self.assertEqual(self.marker()["phase"], "publishing")
        self.assertEqual(self.run_restore(), {"status": "restored"})
        self.assert_restored()

    def test_rename_never_receives_mount_root_path(self):
        original = restore._rename_noreplace
        before = self.root.stat().st_ino

        def guard(src, name, dst):
            self.assertTrue(restore._name(name))
            self.assertEqual(os.fstat(dst).st_ino, before)
            self.assertNotEqual(os.fstat(src).st_ino, before)
            return original(src, name, dst)

        with mock.patch.object(restore, "_rename_noreplace", side_effect=guard):
            self.run_restore()
        self.assertEqual(self.root.stat().st_ino, before)

    def test_database_root_and_child_ownership_preserved(self):
        if os.geteuid() != 0:
            self.skipTest("changing ownership requires root")

        def populate(stage):
            self.populate(stage)
            os.chown(stage, 65534, 65534)
            stage.chmod(0o700)
            for path in (stage / "a", stage / "b", stage / "b" / "nested"):
                os.chown(path, 65534, 65534)

        self.assertEqual(self.run_restore(populate), {"status": "restored"})
        for path in (self.root, self.root / "a", self.root / "b", self.root / "b" / "nested"):
            self.assertEqual((path.stat().st_uid, path.stat().st_gid), (65534, 65534))
        self.assertEqual((self.root / restore.MARKER).stat().st_uid, 0)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)

    def test_live_or_invalid_database_pid_prevents_any_partial_cleanup(self):
        payload = self.interrupted_population()
        for name, raw in (("postmaster.pid", str(os.getpid()).encode() + b"\nignored metadata\n"),
                          ("server.pid", str(os.getpid()).encode() + b"\n"),
                          ("server.pid", b"not-a-pid"), ("postmaster.pid", b"-1\n"),
                          ("server.pid", b"9999999999\n")):
            with self.subTest(name=name, raw=raw):
                pid_file = payload / name
                pid_file.write_bytes(raw)
                self.error("restore_database_busy", self.run_restore)
                self.assertEqual((payload / "partial").read_bytes(), b"synthetic partial")
                self.assertEqual(pid_file.read_bytes(), raw)
                pid_file.unlink()

    def test_database_pid_permission_denied_is_not_proof_stopped(self):
        payload = self.interrupted_population()
        (payload / "server.pid").write_bytes(b"12345\n")
        with mock.patch.object(restore.os, "kill", side_effect=PermissionError(errno.EPERM, "denied")):
            self.error("restore_database_busy", self.run_restore)
        self.assertTrue((payload / "partial").exists())

    def test_definitively_stale_database_pid_allows_private_payload_retry(self):
        payload = self.interrupted_population()
        (payload / "postmaster.pid").write_bytes(b"12345\nignored metadata\n")
        (payload / "server.pid").write_bytes(b"12345\n")
        with mock.patch.object(restore.os, "kill", side_effect=ProcessLookupError(errno.ESRCH, "gone")):
            self.assertEqual(self.run_restore(), {"status": "restored"})
        self.assert_restored()
        self.assertFalse((self.root / "postmaster.pid").exists())

    def test_database_pid_symlink_rejected_without_reading_external_file(self):
        payload = self.interrupted_population()
        external = self.base / "external-pid"
        external.write_bytes(b"preserve")
        (payload / "server.pid").symlink_to(external)
        self.error("restore_database_busy", self.run_restore)
        self.assertTrue((payload / "partial").exists())
        self.assertEqual(external.read_bytes(), b"preserve")

    def test_callback_live_database_pid_is_never_published(self):
        def populate(stage):
            self.populate(stage)
            (stage / "postmaster.pid").write_text(str(os.getpid()) + "\n")
        self.error("restore_database_busy", lambda: self.run_restore(populate))
        self.assertFalse((self.root / "a").exists())
        self.assertEqual(self.marker()["phase"], "populating")

    def test_malformed_journal_field_types_always_fail_safely(self):
        self.interrupted_population()
        original = self.marker()
        for field in original:
            for value in (None, False, 0, -1, [], {}, "", "/", "../bad", ["invalid"]):
                with self.subTest(field=field, value=value):
                    candidate = json.loads(json.dumps(original))
                    candidate[field] = value
                    try:
                        restore._validate_journal(candidate, original["target"])
                    except BackupError:
                        pass
                    except Exception as error:
                        self.fail(f"Unsafe schema error: {type(error).__name__}")

    def test_every_fsync_crash_boundary_is_restartable(self):
        fsync = restore.os.fsync
        counter = [0]

        def count(fd):
            counter[0] += 1
            fsync(fd)

        probe = self.base / "sync-probe"
        probe.mkdir()
        probe_config = SimpleNamespace(data_path=probe, dataset_id="synthetic", env={})
        with mock.patch.object(restore.os, "fsync", side_effect=count):
            self.run_restore(config=probe_config)
        for boundary in range(1, counter[0] + 1):
            with self.subTest(boundary=boundary):
                root = self.base / f"sync-{boundary}"
                root.mkdir()
                config = SimpleNamespace(data_path=root, dataset_id="synthetic", env={})
                seen = [0]

                def crash(fd):
                    seen[0] += 1
                    fsync(fd)
                    if seen[0] == boundary:
                        raise Crash()

                with mock.patch.object(restore.os, "fsync", side_effect=crash):
                    with self.assertRaises(Crash):
                        self.run_restore(config=config)
                result = self.run_restore(config=config)
                self.assertIn(result["status"], {"restored", "skipped"})
                self.assertEqual((root / "a").read_bytes(), b"synthetic-alpha")
                self.assertEqual(json.loads((root / restore.MARKER).read_text())["phase"], "complete")
                self.assertEqual(list(root.glob(".k3s-restic-stage-*")), [])

    def test_validated_phase_allows_repository_independent_publication_resume(self):
        self.assertIsNone(restore.resume_phase(self.config))
        self.interrupted_population()
        self.assertEqual(restore.resume_phase(self.config), "populating")
        self.assertTrue(restore.reset_failed_population(self.config, self.snapshot))
        self.interrupted_publish(after_move=True)
        self.assertEqual(restore.resume_phase(self.config), "publishing")
        pending = restore.resume_snapshot(self.config)
        self.assertEqual(pending, self.snapshot)
        callback = mock.Mock(side_effect=AssertionError("repository must not be contacted"))
        self.assertEqual(self.run_restore(callback, snapshot=pending), {"status": "restored"})
        callback.assert_not_called()
        self.assertEqual(restore.resume_phase(self.config), "complete")
        self.assertIsNone(restore.resume_snapshot(self.config))
        self.assertEqual(self.run_restore(callback), {"status": "skipped", "reason": "already_restored"})
        callback.assert_not_called()

    def test_resume_phase_rejects_untrusted_stage_like_snapshot_probe(self):
        self.interrupted_population()
        stage = self.stage()
        stage.rename(self.base / "retained-stage")
        stage.symlink_to(self.base, target_is_directory=True)
        self.error("restore_ownership", lambda: restore.resume_phase(self.config))

    def test_resume_snapshot_selects_verified_original_request_without_mutating(self):
        self.assertIsNone(restore.resume_snapshot(self.config))
        self.interrupted_population()
        before = (self.root / restore.MARKER).read_bytes()
        self.assertEqual(restore.resume_snapshot(self.config), self.snapshot)
        self.assertEqual((self.root / restore.MARKER).read_bytes(), before)
        self.assertEqual(self.run_restore(), {"status": "restored"})
        self.assertIsNone(restore.resume_snapshot(self.config))

    def test_resume_snapshot_checks_stage_payload_and_dataset_ownership(self):
        payload = self.interrupted_population()
        wrong = SimpleNamespace(data_path=self.root, dataset_id="other", env={})
        self.error("restore_conflict", lambda: restore.resume_snapshot(wrong))
        payload.rename(self.base / "saved-payload")
        payload.symlink_to(self.base, target_is_directory=True)
        self.error("restore_ownership", lambda: restore.resume_snapshot(self.config))
        self.assertTrue((self.base / "saved-payload" / "partial").exists())

    def test_resume_publishing_does_not_reselect_or_repopulate(self):
        self.interrupted_publish(after_move=True)
        self.assertEqual(restore.resume_snapshot(self.config), self.snapshot)
        self.assertEqual(self.run_restore(), {"status": "restored"})

    def test_explicit_reset_unpublished_candidate_enables_older_fallback(self):
        self.interrupted_population()
        self.assertTrue(restore.reset_failed_population(self.config, self.snapshot))
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertIsNone(restore.resume_snapshot(self.config))
        self.assertFalse(restore.reset_failed_population(self.config, self.snapshot))
        self.assertEqual(self.run_restore(snapshot="b" * 64), {"status": "restored"})
        self.assertEqual(self.marker()["snapshot_id"], "b" * 64)

    def test_reset_refuses_publishing_complete_wrong_request_or_unrelated_root(self):
        self.interrupted_population()
        self.error("restore_conflict", lambda: restore.reset_failed_population(self.config, "b" * 64))
        unrelated = self.root / "unrelated"
        unrelated.write_bytes(b"preserve")
        self.error("restore_conflict", lambda: restore.reset_failed_population(self.config, self.snapshot))
        self.assertEqual(unrelated.read_bytes(), b"preserve")
        unrelated.unlink()
        self.assertTrue(restore.reset_failed_population(self.config, self.snapshot))
        self.interrupted_publish(after_move=True)
        self.error("restore_conflict", lambda: restore.reset_failed_population(self.config, self.snapshot))
        self.assertEqual((self.root / "a").read_bytes(), b"synthetic-alpha")
        self.run_restore()
        self.error("restore_conflict", lambda: restore.reset_failed_population(self.config, self.snapshot))
        self.assert_restored()

    def test_reset_checks_database_pid_and_nested_mount_before_deletion(self):
        payload = self.interrupted_population()
        (payload / "server.pid").write_text(str(os.getpid()) + "\n")
        self.error("restore_database_busy", lambda: restore.reset_failed_population(self.config, self.snapshot))
        self.assertTrue((payload / "partial").exists())
        self.assertEqual(self.marker()["phase"], "populating")
        (payload / "server.pid").unlink()
        (payload / "nested-mount").mkdir()
        (payload / "nested-mount" / "sentinel").write_bytes(b"preserve")
        original = restore._mount_id

        def mount(fd):
            if Path(os.readlink(f"/proc/self/fd/{fd}")).name == "nested-mount":
                return original(fd) + 1
            return original(fd)

        with mock.patch.object(restore, "_mount_id", side_effect=mount):
            self.error("restore_mount_boundary", lambda: restore.reset_failed_population(self.config, self.snapshot))
        self.assertTrue((payload / "partial").exists())
        self.assertEqual((payload / "nested-mount" / "sentinel").read_bytes(), b"preserve")
        self.assertEqual(self.marker()["phase"], "populating")

    def test_reset_crashes_at_each_deletion_step_recover_with_new_candidate(self):
        rmdir = restore.os.rmdir
        for point in ("intent", "payload", "stage"):
            with self.subTest(point=point):
                root = self.base / f"reset-{point}"
                root.mkdir()
                config = SimpleNamespace(data_path=root, dataset_id="synthetic", env={})

                def partial(stage):
                    (stage / "partial").write_bytes(b"partial")
                    raise Crash()

                with self.assertRaises(Crash):
                    self.run_restore(partial, config=config)
                save = restore._Journal.save

                def saved(journal, stage):
                    save(journal, stage)
                    if point == "intent" and journal.data["phase"] == "resetting":
                        raise Crash()

                def removed(name, *args, **kwargs):
                    rmdir(name, *args, **kwargs)
                    if ((point == "payload" and name == "payload")
                            or (point == "stage" and str(name).startswith(restore._STAGE_PREFIX))):
                        raise Crash()

                with mock.patch.object(restore._Journal, "save", saved):
                    with mock.patch.object(restore.os, "rmdir", side_effect=removed):
                        with self.assertRaises(Crash):
                            restore.reset_failed_population(config, self.snapshot)
                self.assertIsNone(restore.resume_snapshot(config))
                self.assertEqual(self.run_restore(snapshot="b" * 64, config=config), {"status": "restored"})
                self.assertEqual(json.loads((root / restore.MARKER).read_text())["snapshot_id"], "b" * 64)
                self.assertEqual((root / "a").read_bytes(), b"synthetic-alpha")

    def test_reset_after_intent_can_retry_same_reset_api(self):
        self.interrupted_population()
        save = restore._Journal.save

        def crash(journal, stage):
            save(journal, stage)
            if journal.data["phase"] == "resetting":
                raise Crash()

        with mock.patch.object(restore._Journal, "save", crash):
            with self.assertRaises(Crash):
                restore.reset_failed_population(self.config, self.snapshot)
        self.assertTrue(restore.reset_failed_population(self.config, self.snapshot))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_callback_permission_error_is_safe_and_diagnostic(self):
        def denied(stage):
            raise PermissionError(errno.EPERM, "synthetic-private-error")
        self.error("restore_permissions", lambda: self.run_restore(denied))
        self.assertEqual(self.marker()["phase"], "populating")

    def test_db_helper_traverse_window_interruption_is_recovered(self):
        payload = self.interrupted_population()
        payload.parent.chmod(0o710)
        self.assertEqual(self.run_restore(), {"status": "restored"})
        self.assert_restored()


if __name__ == "__main__":
    unittest.main()
