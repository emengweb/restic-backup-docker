"""User-facing workflow: complete snapshots and unattended latest recovery."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from . import manifest
from .config import boolean
from .errors import BackupError
from .process import run
from .repository import Repository, timestamp
from .scheduler import Cron, in_window
from .limits import job_budget, capacity_budget


def emit(event: str, **safe):
    print(json.dumps({"event": event, **safe}, separators=(",", ":")), flush=True)


class Runner:
    def __init__(self, config, repository=None):
        self.config = config
        self.repo = repository or Repository(config)
        c = self.config
        if c.work_path.is_symlink(): raise BackupError("unsafe_work_path", "Work directory cannot be a symlink")
        c.work_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(c.work_path, 0o700)
        (c.work_path / "status").mkdir(mode=0o700, exist_ok=True)

    @contextmanager
    def lock(self):
        p = self.config.work_path / "job.lock"
        fd = os.open(p, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise BackupError("local_job_busy", "Another local backup or maintenance job is active") from None
            yield
        finally: os.close(fd)

    def state(self, name, data):
        p = self.config.work_path / "status" / (name + ".json")
        tmp = p.with_suffix(".tmp")
        with tmp.open("w") as f:
            os.chmod(tmp, 0o600); json.dump(data, f); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, p)

    def headroom(self):
        if shutil.disk_usage(self.config.work_path).free < self.config.min_free_bytes:
            raise BackupError("insufficient_space", "Backup scratch has insufficient free space")

    def hook(self, action):
        c = self.config
        roots = [Path(c.env.get("HOOKS_DIR", "/hooks")), Path("/custom"), Path("/custem")]
        present = [p / (action + ".sh") for p in roots if (p / (action + ".sh")).is_file()]
        present = list(dict.fromkeys(present))
        if len(present) > 1: raise BackupError("ambiguous_hook", "Multiple hook definitions require explicit HOOKS_DIR")
        if present:
            run(["/bin/sh", str(present[0])], env=c.env, timeout=c.run_timeout)

    def backup(self, now=None):
        c = self.config
        now = now or datetime.now(ZoneInfo(c.timezone))
        if c.engine != "files" and c.env.get("BACKUP_MODE") == "dual":
            if not in_window(c.backup_window, now):
                return {"status": "skipped_window", "time": now.isoformat(), "exported": False}
            from dataclasses import replace
            results = {}
            # Both native streams use one image, the same dataset and original schedule/window.
            # Each stream has a stable path; neither can masquerade as the other.
            with self.lock(), job_budget(c.run_timeout, c.max_bytes):
                for mode in ["logical", "physical"]:
                    child = replace(c, env=dict(c.env, BACKUP_MODE=mode), work_path=c.work_path / mode)
                    results[mode] = Runner(child).backup(now)
            result = {"status": "backed_up", "exported": True, "streams": results, "time": datetime.now(timezone.utc).isoformat()}
            self.state("last_success", result); self.state("last_attempt", result)
            return result
        if c.engine != "files" and not in_window(c.backup_window, now):
            s = {"status": "skipped_window", "time": now.isoformat(), "exported": False}
            self.state("last_attempt", s)
            return s
        with self.lock(), job_budget(c.run_timeout, c.max_bytes):
            self.headroom(); self.repo.probe(); self.hook("pre-backup")
            # No concurrent publisher holds job.lock; only our orphan export dirs are removable.
            for abandoned in c.work_path.glob(".export-*"):
                if abandoned.is_dir() and not abandoned.is_symlink(): shutil.rmtree(abandoned)
            stage = Path(tempfile.mkdtemp(prefix=".export-", dir=c.work_path))
            try:
                if c.engine == "files":
                    if not c.data_path.is_dir() or c.data_path.is_symlink():
                        raise BackupError("data_missing", "Source data directory is missing or unsafe")
                    # Restic reads the source directly, without duplicating the full volume into scratch.
                    metadata = {"engine": "files", "consistency": "file-copy", "source_root": str(c.data_path)}
                else:
                    from .engines import export_database, export_physical
                    with capacity_budget([stage], c.max_bytes, c.min_free_bytes):
                        metadata = export_physical(c, stage) if c.env.get("BACKUP_MODE") == "physical" else export_database(c, stage)
                manifest.create(stage, c, metadata, c.data_path if c.engine == "files" else None)
                if c.engine != "files": manifest.verify(stage, c)
                current = c.work_path / "current"
                old = c.work_path / "previous"
                if old.exists(): shutil.rmtree(old)
                if current.exists(): os.replace(current, old)
                os.replace(stage, current)
                ident = self.repo.backup(current, c.data_path) if c.engine == "files" else self.repo.backup(current)
                # Files are authenticated by restic trees; native dumps verified above.
                s = {"status": "backed_up", "snapshot_id": ident, "time": datetime.now(timezone.utc).isoformat(), "exported": True}
                self.state("last_success", s); self.state("last_attempt", s)
                self.hook("post-backup")
                return s
            finally:
                if stage.exists(): shutil.rmtree(stage)

    def _copy_payload(self, src, dst):
        for child in src.iterdir():
            if child.name.startswith(".k3s-restic-"): continue
            target = dst / child.name
            if child.is_symlink():
                manifest.safe_symlink(src, child, os.readlink(child)); target.symlink_to(os.readlink(child))
            elif child.is_dir(): shutil.copytree(child, target, symlinks=True)
            elif child.is_file(): shutil.copy2(child, target)
            else: raise BackupError("special_file", "Unsupported special file in restored content")
        if os.geteuid() == 0:
            for parent, dirs, files in os.walk(src, followlinks=False):
                for name in dirs + files:
                    a = Path(parent) / name; b = dst / a.relative_to(src)
                    if b.exists() or b.is_symlink():
                        st = a.lstat(); os.chown(b, st.st_uid, st.st_gid, follow_symlinks=False)

    def _populate(self, snapshot, scratch, dest):
        c = self.config
        self.repo.restore(snapshot, scratch)
        root = self.repo.envelope(snapshot, scratch)
        mode = snapshot["_mode"]
        if mode == "manifest":
            if c.engine == "files" and len(snapshot.get("paths") or []) == 2:
                raw = json.loads((root / manifest.NAME).read_text())
                origin = raw.get("source_path", "")
                if origin not in snapshot["paths"] or not origin.startswith("/") or ".." in Path(origin).parts:
                    raise BackupError("payload_missing", "File backup source path is invalid")
                payload = scratch.joinpath(*Path(origin).parts[1:])
                m = manifest.verify(root, c, payload)
            else:
                m = manifest.verify(root, c); payload = root / "payload"
            if c.engine == "files":
                if not payload.is_dir() or payload.is_symlink(): raise BackupError("payload_missing", "File backup payload is missing")
                self._copy_payload(payload, dest)
            else:
                from .engines import restore_database, validate_physical
                if m.get("kind") in {"pg-physical", "mariadb-physical"}:
                    source = root / str(m.get("engine", {}).get("payload", "payload"))
                    if source.is_symlink() or not source.is_dir() or root.resolve() not in source.resolve().parents:
                        raise BackupError("unsafe_physical_payload", "Physical backup payload is unsafe")
                    self._copy_payload(source, dest)
                    validate_physical(c, dest, m)
                else:
                    restore_database(c, root, dest, m)
        elif mode == "legacy-files":
            manifest.inspect(root, c.max_bytes)
            self._copy_payload(root, dest)
        elif mode == "legacy-physical":
            from .engines import validate_physical
            validate_physical(c, root, None)
            self._copy_payload(root, dest)
        elif mode == "legacy-affine-native":
            from .engines import legacy_manifest, restore_database
            metadata = legacy_manifest(c, root)
            m = {"schema_version": 1, "dataset_id": c.dataset_id, "kind": "pg-logical", "complete": True, "engine": metadata}
            restore_database(c, root, dest, m)
        else:
            raise BackupError("unsupported_restore_format", "Backup format is not supported")

    def restore(self):
        from .restore import restore_to_volume, resume_snapshot, resume_phase, reset_failed_population
        c = self.config
        with self.lock(), job_budget(c.run_timeout, c.max_bytes):
            phase = resume_phase(c)
            if phase == "publishing":
                pending = resume_snapshot(c)
                def no_network_population(_):
                    raise BackupError("invalid_publication_state", "Validated publication must not repopulate its payload")
                return restore_to_volume(c, pending, no_network_population)
            # Existing application data must not depend on repo availability at ordinary restart.
            reserved = {".k3s-restic-restore.json"}
            if c.data_path.is_dir() and not c.data_path.is_symlink():
                complete_marker = c.data_path / ".k3s-restic-restore.json"
                if complete_marker.is_file() and not complete_marker.is_symlink():
                    try: completed = json.loads(complete_marker.read_text())
                    except (OSError, ValueError): completed = {}
                    if completed.get("phase") == "complete" and completed.get("dataset_id") == c.dataset_id:
                        return restore_to_volume(c, completed.get("snapshot_id", ""), lambda _: None)
                entries = [p for p in c.data_path.iterdir() if p.name not in reserved and not p.name.startswith(".k3s-restic-stage-") and p.name != "lost+found"]
                marker = c.data_path / ".k3s-restic-restore.json"
                if entries and not marker.exists(): return {"status": "skipped", "reason": "nonempty"}
            self.headroom()
            self.repo.probe()
            pending = resume_snapshot(c)
            candidates = self.repo.candidates(resume_id=pending)
            if pending:
                selected = next((s for s in candidates if s["id"] == pending), None)
                if selected is None:
                    raise BackupError("pending_snapshot_unavailable", "Interrupted recovery snapshot is unavailable; existing stage was preserved")
                candidates = [selected] + [s for s in candidates if s["id"] != pending]
            if not candidates: raise BackupError("no_restore_candidate", "No complete compatible backup is available; empty application startup is blocked")
            failures = []
            for candidate in candidates:
                scratch = Path(tempfile.mkdtemp(prefix=".restore-", dir=c.work_path))
                try:
                    def populate_with_budget(dest):
                        if shutil.disk_usage(dest).free < c.min_free_bytes:
                            raise BackupError("insufficient_space", "Restore destination has insufficient free space")
                        with capacity_budget([scratch, dest], c.max_bytes * 2, c.min_free_bytes):
                            self._populate(candidate, scratch, dest)
                    result = restore_to_volume(c, candidate["id"], populate_with_budget)
                    result.update(snapshot_id=candidate["id"], snapshot_time=candidate["time"], fallback_count=len(failures))
                    self.state("last_restore", result)
                    return result
                except BackupError as e:
                    # Do not turn auth, I/O, space, ownership or ambiguous target errors into a stale restore.
                    can_fallback = {"artifact_mismatch", "invalid_manifest", "manifest_missing", "unsupported_version", "physical_version_mismatch", "payload_missing", "snapshot_corrupt"}
                    failures.append(e.code)
                    if e.code not in can_fallback or not boolean(c.env.get("RESTORE_FALLBACK"), True): raise
                    reset_failed_population(c, candidate["id"])
                finally: shutil.rmtree(scratch)
            raise BackupError("no_usable_backup", "All compatible automatic recovery candidates failed verification")

    def check(self):
        c = self.config
        with self.lock(), job_budget(c.run_timeout, c.max_bytes):
            result = self.repo.check(); result["time"] = datetime.now(timezone.utc).isoformat()
            self.state("last_check", result); return result

    def prune(self, apply=False):
        c = self.config
        with self.lock(), job_budget(c.run_timeout, c.max_bytes): return self.repo.prune(apply=apply)

    def health(self):
        p = self.config.work_path / "status" / "last_success.json"
        if not p.exists(): return {"status": "not_yet_backed_up"}
        try: state = json.loads(p.read_text())
        except ValueError: raise BackupError("invalid_status", "Backup health state is unreadable") from None
        age = (datetime.now(timezone.utc) - timestamp(state["time"])).total_seconds()
        return {"status": "healthy" if age < int(self.config.env.get("BACKUP_STALE_SECONDS", "90000")) else "stale", "age_seconds": int(age)}

    def schedule(self):
        c = self.config
        jobs = [(Cron(c.backup_cron), self.backup)]
        if c.check_cron: jobs.append((Cron(c.check_cron), self.check))
        if c.prune_cron: jobs.append((Cron(c.prune_cron), lambda: self.prune(apply=boolean(c.env.get("RETENTION_APPLY")))))
        previous = None
        while True:
            now = datetime.now(ZoneInfo(c.timezone)); key = now.strftime("%Y-%m-%dT%H:%M%z")
            if key != previous:
                previous = key
                for cron, fn in jobs:
                    if cron.matches(now):
                        try: emit("job", **fn())
                        except BackupError as e:
                            self.state("last_failure", {"code": e.code, "time": now.isoformat()}); emit("failure", code=e.code)
            time.sleep(1)
