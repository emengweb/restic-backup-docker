"""Crash-safe no-clobber publication into an existing, possibly mounted volume.

The callback receives only an empty payload directory. A private wrapper and
journal belong to this module. Recovery never deletes application-root entries.
Linux renameat2(RENAME_NOREPLACE), flock, fsync and proc mount IDs are required;
we fail closed rather than overwriting data or traversing a bind mount.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
import re
import stat
import uuid
from collections import deque
from pathlib import Path
from typing import Callable

from .errors import BackupError

MARKER = ".k3s-restic-restore.json"
_PREFIX = ".k3s-restic-"
_STAGE_PREFIX = _PREFIX + "stage-"
_NEXT = "journal-next"
_PAYLOAD = "payload"
_MAX_JOURNAL = 16 * 1024 * 1024
_MAX_ENTRIES = 100_000
_MAX_DEPTH = 128
_MIN_FREE = 1024 * 1024
_PHASES = {"allocating", "populating", "publishing", "complete", "resetting"}
_STAGE_RE = re.compile(r"^\.k3s-restic-stage-[0-9a-f]{32}$")


def _fail(code: str, message: str) -> None:
    raise BackupError(code, message)


def _integer(value: object, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _name(value: object) -> bool:
    if (not isinstance(value, str) or not value or value in {".", ".."}
            or "/" in value or "\x00" in value or value.startswith(_PREFIX)):
        return False
    try:
        return len(os.fsencode(value)) <= 255
    except UnicodeEncodeError:
        return False


def _kind(mode: int) -> str:
    for check, kind in ((stat.S_ISDIR, "directory"), (stat.S_ISREG, "file"),
                        (stat.S_ISLNK, "symlink")):
        if check(mode):
            return kind
    _fail("restore_unsafe_entry", "Restore contains an unsupported filesystem entry")


def _identity(info: os.stat_result) -> dict:
    return {"dev": info.st_dev, "ino": info.st_ino, "kind": _kind(info.st_mode)}


def _valid_identity(value: object, kind: str | None = None) -> bool:
    return (isinstance(value, dict) and set(value) == {"dev", "ino", "kind"}
            and _integer(value["dev"]) and _integer(value["ino"], 1)
            and isinstance(value["kind"], str)
            and value["kind"] in {"directory", "file", "symlink"}
            and (kind is None or value["kind"] == kind))


def _mount_id(fd: int) -> int:
    # mnt_id also detects same-device bind mounts, unlike st_dev/ismount.
    try:
        with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as stream:
            for line in stream:
                if line.startswith("mnt_id:"):
                    return int(line.split(":", 1)[1].strip())
    except (OSError, ValueError):
        pass
    _fail("restore_mount_check", "Cannot establish filesystem mount boundaries")


def _describe(parent: int, name: str, mount: int) -> os.stat_result | None:
    try:
        fd = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    except FileNotFoundError:
        return None
    try:
        if _mount_id(fd) != mount:
            _fail("restore_mount_boundary", "Restore must not traverse or remove a nested mount")
        return os.fstat(fd)
    finally:
        os.close(fd)


def _owned(parent: int, name: str, expected: dict, mount: int) -> os.stat_result:
    info = _describe(parent, name, mount)
    if info is None or _identity(info) != expected:
        _fail("restore_ownership", "Restore entry identity changed; existing data was preserved")
    return info


def _open_dir(parent: int, name: str, expected: dict, mount: int) -> int:
    _owned(parent, name, expected, mount)
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                 dir_fd=parent)
    if _identity(os.fstat(fd)) != expected or _mount_id(fd) != mount:
        os.close(fd)
        _fail("restore_ownership", "Restore directory identity changed")
    return fd


def _target(path: Path) -> int:
    if (not path.is_absolute() or ".." in path.parts or len(path.parts) < 2
            or "\x00" in os.fspath(path)):
        _fail("restore_path", "Restore target must be an existing absolute non-root directory")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _same_target(path: Path, expected: dict, mount: int) -> None:
    fd = _target(path)
    try:
        if _identity(os.fstat(fd)) != expected or _mount_id(fd) != mount:
            _fail("restore_path", "Restore target path changed during recovery")
    finally:
        os.close(fd)


def _space(fd: int, reserve: int, needed: int = 0) -> None:
    info = os.fstatvfs(fd)
    if info.f_flag & getattr(os, "ST_RDONLY", 1):
        _fail("restore_readonly", "Restore target filesystem is read-only")
    if info.f_bavail * info.f_frsize < max(reserve, needed) or info.f_favail == 0:
        _fail("restore_no_space", "Insufficient free space or inodes for safe restore publication")


def _rename_noreplace(src: int, name: str, dst: int) -> None:
    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError):
        _fail("restore_atomic_unsupported", "Atomic no-clobber publication is unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                       ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(src, os.fsencode(name), dst, os.fsencode(name), 1) != 0:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            _fail("restore_conflict", "A restore destination appeared; existing data was preserved")
        if code in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
            _fail("restore_atomic_unsupported", "Filesystem lacks atomic no-clobber publication")
        raise OSError(code, "Restore publication failed")


def _pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("restore_marker", "Restore journal has duplicate fields")
        result[key] = value
    return result


def _private_file(info: os.stat_result | None) -> bool:
    return (info is not None and stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid() and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600)


def _validate_journal(journal: object, target: dict) -> dict:
    keys = {"schema_version", "dataset_id", "snapshot_id", "phase", "owner_uid", "target",
            "stage", "stage_identity", "payload_identity", "owned_entries", "root_metadata"}
    if not isinstance(journal, dict) or set(journal) != keys:
        _fail("restore_marker", "Restore journal schema is invalid")
    if (type(journal["schema_version"]) is not int or journal["schema_version"] != 1
            or not isinstance(journal["phase"], str) or journal["phase"] not in _PHASES
            or not _integer(journal["owner_uid"]) or journal["owner_uid"] != os.geteuid()
            or not _valid_identity(journal["target"], "directory") or journal["target"] != target
            or not isinstance(journal["stage"], str) or not _STAGE_RE.fullmatch(journal["stage"])):
        _fail("restore_marker", "Restore journal identity or ownership is invalid")
    for field in ("dataset_id", "snapshot_id"):
        value = journal[field]
        if not isinstance(value, str) or not value or len(value) > 512 or any(ord(c) < 32 for c in value):
            _fail("restore_marker", "Restore journal request identity is invalid")
    for field in ("stage_identity", "payload_identity"):
        if journal[field] is not None and not _valid_identity(journal[field], "directory"):
            _fail("restore_marker", "Restore journal staging identity is invalid")
    entries = journal["owned_entries"]
    if not isinstance(entries, list) or len(entries) > _MAX_ENTRIES:
        _fail("restore_marker", "Restore journal entry list is invalid")
    names = set()
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != {"name", "identity", "published"}
                or not _name(entry["name"]) or entry["name"] in names
                or not _valid_identity(entry["identity"]) or type(entry["published"]) is not bool):
            _fail("restore_marker", "Restore journal contains an unsafe owned entry")
        names.add(entry["name"])
    metadata = journal["root_metadata"]
    if metadata is not None and (
        not isinstance(metadata, dict) or set(metadata) != {"uid", "gid", "mode"}
        or not all(_integer(metadata[key]) for key in ("uid", "gid", "mode"))
        or metadata["uid"] >= 2**32 - 1 or metadata["gid"] >= 2**32 - 1
        or metadata["mode"] > 0o7777
    ):
        _fail("restore_marker", "Restore journal root permissions are invalid")
    phase = journal["phase"]
    if phase not in {"allocating", "resetting"} and (journal["stage_identity"] is None or journal["payload_identity"] is None):
        _fail("restore_marker", "Restore journal lacks staging ownership")
    if ((phase in {"allocating", "populating", "resetting"} and (entries or metadata is not None))
            or (phase in {"publishing", "complete"} and metadata is None)
            or (phase == "complete" and not all(entry["published"] for entry in entries))
            or (journal["payload_identity"] is not None and journal["stage_identity"] is None)):
        _fail("restore_marker", "Restore journal state is inconsistent")
    return journal


class _Journal:
    def __init__(self, target: int, mount: int, identity: dict, reserve: int):
        self.target, self.mount, self.identity, self.reserve = target, mount, identity, reserve
        self.marker_identity: dict | None = None
        self.data: dict = {}

    def read(self) -> bool:
        info = _describe(self.target, MARKER, self.mount)
        if info is None:
            return False
        if not _private_file(info) or not 0 < info.st_size <= _MAX_JOURNAL:
            _fail("restore_marker", "Restore journal is not a trusted private regular file")
        fd = os.open(MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=self.target)
        try:
            if os.fstat(fd) != info:
                _fail("restore_marker", "Restore journal changed while being opened")
            chunks, remaining = [], _MAX_JOURNAL + 1
            while remaining:
                block = os.read(fd, min(remaining, 65536))
                if not block:
                    break
                chunks.append(block)
                remaining -= len(block)
            raw = b"".join(chunks)
            if len(raw) > _MAX_JOURNAL:
                _fail("restore_marker", "Restore journal exceeds the size limit")
            try:
                data = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
            except (ValueError, UnicodeError, RecursionError):
                _fail("restore_marker", "Restore journal is incomplete or invalid")
        finally:
            os.close(fd)
        self.marker_identity = _identity(info)
        _owned(self.target, MARKER, self.marker_identity, self.mount)
        self.data = _validate_journal(data, self.identity)
        return True

    def _bytes(self) -> bytes:
        raw = (json.dumps(self.data, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
               + "\n").encode("utf-8")
        if len(raw) > _MAX_JOURNAL:
            _fail("restore_marker", "Restore journal exceeds the size limit")
        return raw

    def create(self, dataset: str, snapshot: str) -> None:
        self.data = {"schema_version": 1, "dataset_id": dataset, "snapshot_id": snapshot,
                     "phase": "allocating", "owner_uid": os.geteuid(), "target": self.identity,
                     "stage": _STAGE_PREFIX + uuid.uuid4().hex, "stage_identity": None,
                     "payload_identity": None, "owned_entries": [], "root_metadata": None}
        raw = self._bytes()
        _space(self.target, self.reserve, len(raw) * 2 + 4096)
        fd = os.open(MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=self.target)
        try:
            os.fchmod(fd, 0o600)
            self.marker_identity = _identity(os.fstat(fd))
            _write_all(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(self.target)

    def clear_next(self, stage: int) -> None:
        info = _describe(stage, _NEXT, self.mount)
        if info is None:
            return
        if not _private_file(info):
            _fail("restore_marker", "Restore journal temporary file is unsafe")
        _owned(stage, _NEXT, _identity(info), self.mount)
        os.unlink(_NEXT, dir_fd=stage)
        os.fsync(stage)

    def save(self, stage: int) -> None:
        marker = _owned(self.target, MARKER, self.marker_identity, self.mount)
        if not _private_file(marker):
            _fail("restore_marker", "Restore journal permissions changed")
        self.clear_next(stage)
        raw = self._bytes()
        _space(self.target, self.reserve, len(raw) * 2 + 4096)
        fd = os.open(_NEXT, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=stage)
        try:
            os.fchmod(fd, 0o600)
            _write_all(fd, raw)
            os.fsync(fd)
            next_identity = _identity(os.fstat(fd))
        finally:
            os.close(fd)
        _owned(self.target, MARKER, self.marker_identity, self.mount)
        os.replace(_NEXT, MARKER, src_dir_fd=stage, dst_dir_fd=self.target)
        self.marker_identity = next_identity
        os.fsync(stage)
        os.fsync(self.target)


def _write_all(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "Restore journal write failed")
        view = view[written:]


def _stage(journal: _Journal, *, missing_ok: bool = False) -> int | None:
    data = journal.data
    info = _describe(journal.target, data["stage"], journal.mount)
    expected = data["stage_identity"]
    if info is None:
        if missing_ok:
            return None
        if expected is not None:
            _fail("restore_ownership", "Owned restore staging directory is missing")
        os.mkdir(data["stage"], 0o700, dir_fd=journal.target)
        os.fsync(journal.target)
        info = _describe(journal.target, data["stage"], journal.mount)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) not in {0o700, 0o710, 0o2700}
            or (expected is not None and _identity(info) != expected)):
        _fail("restore_ownership", "Restore staging directory is untrusted")
    fd = _open_dir(journal.target, data["stage"], _identity(info), journal.mount)
    try:
        # Recover a DB helper interruption in its temporary gid-traverse window.
        os.fchmod(fd, 0o700)
        os.fsync(fd)
        if expected is None:
            if set(os.listdir(fd)) - {_NEXT}:
                _fail("restore_ownership", "Unrecorded restore staging is not empty")
            journal.clear_next(fd)
            data["stage_identity"] = _identity(info)
            journal.save(fd)
        else:
            journal.clear_next(fd)
        if set(os.listdir(fd)) - {_PAYLOAD}:
            _fail("restore_ownership", "Restore staging contains unexpected entries")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _payload(journal: _Journal, stage: int) -> int:
    data = journal.data
    expected = data["payload_identity"]
    info = _describe(stage, _PAYLOAD, journal.mount)
    if info is None:
        if expected is not None:
            _fail("restore_ownership", "Owned restore payload directory is missing")
        os.mkdir(_PAYLOAD, 0o700, dir_fd=stage)
        os.fsync(stage)
        info = _describe(stage, _PAYLOAD, journal.mount)
    if not stat.S_ISDIR(info.st_mode) or (expected is not None and _identity(info) != expected):
        _fail("restore_ownership", "Restore payload directory is untrusted")
    fd = _open_dir(stage, _PAYLOAD, _identity(info), journal.mount)
    if expected is None:
        try:
            if (os.listdir(fd) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) not in {0o700, 0o2700}):
                _fail("restore_ownership", "Unrecorded restore payload is not empty and private")
            data["payload_identity"] = _identity(info)
            journal.save(stage)
        except BaseException:
            os.close(fd)
            raise
    return fd


def _root_entries(journal: _Journal) -> None:
    allowed = {MARKER, journal.data["stage"]}
    if journal.data["phase"] == "publishing":
        allowed.update(entry["name"] for entry in journal.data["owned_entries"])
    if set(os.listdir(journal.target)) - allowed:
        _fail("restore_conflict", "Unrelated data appeared in the restore volume and was preserved")


def _walk(fd: int, mount: int, links: dict, prefix: tuple = (), *, sync: bool = True,
          budget: list | None = None) -> None:
    budget = [_MAX_ENTRIES] if budget is None else budget
    if len(prefix) > _MAX_DEPTH:
        _fail("restore_unsafe_entry", "Restore directory nesting exceeds the safety limit")
    for name in sorted(os.listdir(fd)):
        if not prefix and not _name(name):
            _fail("restore_unsafe_entry", "Restore payload uses a reserved metadata name")
        budget[0] -= 1
        if budget[0] < 0:
            _fail("restore_unsafe_entry", "Restore entry count exceeds the safety limit")
        _walk_entry(fd, name, mount, links, prefix + (name,), sync, budget)
    if sync:
        os.fsync(fd)


def _walk_entry(parent: int, name: str, mount: int, links: dict, logical: tuple,
                sync: bool, budget: list) -> None:
    info = _describe(parent, name, mount)
    if info is None:
        _fail("restore_ownership", "Restore payload changed during validation")
    identity = _identity(info)
    kind = identity["kind"]
    if kind == "directory":
        child = _open_dir(parent, name, identity, mount)
        try:
            _walk(child, mount, links, logical, sync=sync, budget=budget)
        finally:
            os.close(child)
    elif kind == "symlink":
        links[logical] = os.readlink(name, dir_fd=parent)
        _owned(parent, name, identity, mount)
    elif sync:
        child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                        dir_fd=parent)
        try:
            if _identity(os.fstat(child)) != identity or _mount_id(child) != mount:
                _fail("restore_ownership", "Restore file changed during validation")
            os.fsync(child)
        finally:
            os.close(child)


def _links_safe(links: dict) -> None:
    # Resolve the in-memory link graph; never follow a link into the real host.
    for logical, target in links.items():
        if not target or target.startswith("/"):
            _fail("restore_symlink", "Restore symlinks must remain relative to the application tree")
        pending = deque((*logical[:-1], *target.split("/")))
        resolved, hops = [], 0
        while pending:
            part = pending.popleft()
            if part in {"", "."}:
                continue
            if part == "..":
                if not resolved:
                    _fail("restore_symlink", "Restore symlink escapes the application tree")
                resolved.pop()
                continue
            if not resolved and part.startswith(_PREFIX):
                _fail("restore_symlink", "Restore symlink references private restore metadata")
            resolved.append(part)
            link = links.get(tuple(resolved))
            if link is not None:
                hops += 1
                if hops > 40 or link.startswith("/"):
                    _fail("restore_symlink", "Restore symlink graph is unsafe or cyclic")
                resolved.pop()
                pending.extendleft(reversed(link.split("/")))


def _remove_entry(parent: int, name: str, mount: int) -> None:
    # Called ONLY under a validated recorded private payload, never volume root.
    info = _describe(parent, name, mount)
    if info is None:
        _fail("restore_ownership", "Interrupted restore payload changed during cleanup")
    identity = _identity(info)
    if identity["kind"] == "directory":
        child = _open_dir(parent, name, identity, mount)
        try:
            for nested in os.listdir(child):
                _remove_entry(child, nested, mount)
            os.fsync(child)
        finally:
            os.close(child)
        _owned(parent, name, identity, mount)
        os.rmdir(name, dir_fd=parent)
    else:
        _owned(parent, name, identity, mount)
        os.unlink(name, dir_fd=parent)


def _inactive_database(payload: int, mount: int) -> None:
    # A killed restorer can leave a DB child shutting down. Never clear an
    # active datadir; EPERM, malformed PID or identity races are not proof dead.
    # Only these adapter-owned, fixed-name metadata files are read, bounded.
    for name in ("postmaster.pid", "server.pid"):
        info = _describe(payload, name, mount)
        if info is None:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= 4096:
            _fail("restore_database_busy", "Cannot prove the interrupted database is stopped")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=payload)
        try:
            if os.fstat(fd) != info:
                _fail("restore_database_busy", "Database process metadata changed during recovery")
            raw = os.read(fd, 4097)
        finally:
            os.close(fd)
        _owned(payload, name, _identity(info), mount)
        first = raw.split(b"\n", 1)[0] if name == "postmaster.pid" else raw.rstrip(b"\n")
        if len(raw) > 4096 or not re.fullmatch(rb"[1-9][0-9]{0,9}", first):
            _fail("restore_database_busy", "Interrupted database process metadata is invalid")
        pid = int(first)
        if pid > 2**31 - 1:
            _fail("restore_database_busy", "Interrupted database process metadata is invalid")
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except OSError:
            _fail("restore_database_busy", "Cannot prove the interrupted database is stopped")
        _fail("restore_database_busy", "An interrupted database process is still active")


def _reset_payload(journal: _Journal, payload: int) -> None:
    # Read-only boundary scan before deleting even private partial staging.
    _inactive_database(payload, journal.mount)
    _walk(payload, journal.mount, {}, sync=False)
    for name in os.listdir(payload):
        _remove_entry(payload, name, journal.mount)
    os.fsync(payload)


def _locations(journal: _Journal, payload: int) -> list:
    locations = []
    for entry in journal.data["owned_entries"]:
        name, identity = entry["name"], entry["identity"]
        source = _describe(payload, name, journal.mount)
        destination = _describe(journal.target, name, journal.mount)
        if source is not None and destination is not None:
            _fail("restore_conflict", "Restore source and destination both exist; neither was removed")
        if destination is not None:
            if _identity(destination) != identity:
                _fail("restore_ownership", "Restore destination was replaced; existing data was preserved")
            locations.append((entry, journal.target))
        elif source is not None and not entry["published"]:
            if _identity(source) != identity:
                _fail("restore_ownership", "Restore staged entry was replaced")
            locations.append((entry, payload))
        else:
            _fail("restore_ownership", "Owned restore entry is missing or its journal is inconsistent")
    if set(os.listdir(payload)) - {entry["name"] for entry in journal.data["owned_entries"]}:
        _fail("restore_ownership", "Restore staging contains unrecorded application entries")
    return locations


def _publish(journal: _Journal, stage: int, payload: int) -> None:
    _root_entries(journal)
    locations = _locations(journal, payload)
    links, budget = {}, [_MAX_ENTRIES]
    for entry, parent in locations:
        _walk_entry(parent, entry["name"], journal.mount, links, (entry["name"],), True, budget)
    _links_safe(links)
    os.fsync(payload)
    for entry, parent in locations:
        _root_entries(journal)
        _owned(parent, entry["name"], entry["identity"], journal.mount)
        if parent == payload:
            _rename_noreplace(payload, entry["name"], journal.target)
        # BOTH rename sides must reach disk before the journal says published.
        os.fsync(payload)
        os.fsync(journal.target)
        if not entry["published"]:
            entry["published"] = True
            journal.save(stage)
    _root_entries(journal)
    metadata = journal.data["root_metadata"]
    current = os.fstat(journal.target)
    if (current.st_uid, current.st_gid) != (metadata["uid"], metadata["gid"]):
        os.fchown(journal.target, metadata["uid"], metadata["gid"])
    os.fchmod(journal.target, metadata["mode"])
    os.fsync(journal.target)
    journal.data["phase"] = "complete"
    journal.save(stage)


def _cleanup_complete(journal: _Journal, stage: int | None) -> None:
    if stage is None:
        return
    info = _describe(stage, _PAYLOAD, journal.mount)
    if info is not None:
        payload = _open_dir(stage, _PAYLOAD, journal.data["payload_identity"], journal.mount)
        try:
            if os.listdir(payload):
                _fail("restore_ownership", "Completed restore retains unexpected staged application data")
            os.fsync(payload)
        finally:
            os.close(payload)
        _owned(stage, _PAYLOAD, journal.data["payload_identity"], journal.mount)
        os.rmdir(_PAYLOAD, dir_fd=stage)
        os.fsync(stage)
    if os.listdir(stage):
        _fail("restore_ownership", "Completed restore staging is not empty")
    _owned(journal.target, journal.data["stage"], journal.data["stage_identity"], journal.mount)
    os.rmdir(journal.data["stage"], dir_fd=journal.target)
    os.fsync(journal.target)


def _remove_marker(journal: _Journal) -> None:
    info = _owned(journal.target, MARKER, journal.marker_identity, journal.mount)
    if not _private_file(info):
        _fail("restore_marker", "Restore journal permissions changed during reset")
    os.unlink(MARKER, dir_fd=journal.target)
    os.fsync(journal.target)


def _finish_reset(journal: _Journal) -> None:
    if journal.data["phase"] != "resetting" or journal.data["owned_entries"]:
        _fail("restore_conflict", "Only an explicitly abandoned unpublished restore can be reset")
    _root_entries(journal)
    info = _describe(journal.target, journal.data["stage"], journal.mount)
    if info is None:
        _remove_marker(journal)
        return
    stage = _stage(journal)
    payload = None
    try:
        info = _describe(stage, _PAYLOAD, journal.mount)
        if info is not None:
            payload = _payload(journal, stage)
            _reset_payload(journal, payload)
            os.close(payload)
            payload = None
            _owned(stage, _PAYLOAD, journal.data["payload_identity"], journal.mount)
            os.rmdir(_PAYLOAD, dir_fd=stage)
            os.fsync(stage)
        if os.listdir(stage):
            _fail("restore_ownership", "Abandoned restore staging is not empty")
        _root_entries(journal)
        _owned(journal.target, journal.data["stage"], journal.data["stage_identity"], journal.mount)
        os.rmdir(journal.data["stage"], dir_fd=journal.target)
        os.fsync(journal.target)
        _remove_marker(journal)
    finally:
        if payload is not None:
            os.close(payload)
        os.close(stage)


def _locked_journal(config) -> tuple:
    fd = _target(Path(config.data_path))
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _fail("restore_busy", "Another restore owns this volume")
        identity = _identity(os.fstat(fd))
        reserve = int(getattr(config, "env", {}).get("RESTORE_MIN_FREE_BYTES", _MIN_FREE))
        if reserve < _MIN_FREE:
            _fail("restore_config", "Restore free-space reserve cannot be below 1MiB")
        journal = _Journal(fd, _mount_id(fd), identity, reserve)
        exists = journal.read()
        if exists and journal.data["dataset_id"] != config.dataset_id:
            _fail("restore_conflict", "Restore journal belongs to a different dataset")
        return fd, journal, exists
    except BaseException:
        os.close(fd)
        raise


def _resume_state(config) -> tuple[str, str] | None:
    """Return the verified journal phase and original request ID.

    This read-only probe takes the volume lock and checks recorded directory
    identities without creating/reclaiming anything. A completed or explicitly
    abandoned restore has no pending snapshot. The ID is internal, not a log.
    """
    fd = stage = payload = None
    try:
        fd, journal, exists = _locked_journal(config)
        if not exists:
            return None
        if journal.data["phase"] in {"complete", "resetting"}:
            return journal.data["phase"], journal.data["snapshot_id"]
        _root_entries(journal)
        data = journal.data
        info = _describe(fd, data["stage"], journal.mount)
        if info is None:
            if data["stage_identity"] is not None:
                _fail("restore_ownership", "Owned restore staging directory is missing")
            return data["phase"], data["snapshot_id"]
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) not in {0o700, 0o710, 0o2700}
                or (data["stage_identity"] is not None and _identity(info) != data["stage_identity"])):
            _fail("restore_ownership", "Restore staging directory is untrusted")
        stage = _open_dir(fd, data["stage"], _identity(info), journal.mount)
        names = set(os.listdir(stage))
        if (names - {_NEXT, _PAYLOAD}
                or (data["stage_identity"] is None and _PAYLOAD in names)):
            _fail("restore_ownership", "Restore staging contains unexpected entries")
        if _NEXT in names and not _private_file(_describe(stage, _NEXT, journal.mount)):
            _fail("restore_marker", "Restore journal temporary file is unsafe")
        info = _describe(stage, _PAYLOAD, journal.mount)
        if info is None:
            if data["payload_identity"] is not None:
                _fail("restore_ownership", "Owned restore payload directory is missing")
        else:
            if not stat.S_ISDIR(info.st_mode):
                _fail("restore_ownership", "Restore payload directory is untrusted")
            expected = data["payload_identity"]
            payload = _open_dir(stage, _PAYLOAD, expected or _identity(info), journal.mount)
            if expected is None and (os.listdir(payload) or info.st_uid != os.geteuid()
                                     or stat.S_IMODE(info.st_mode) not in {0o700, 0o2700}):
                _fail("restore_ownership", "Unrecorded restore payload is not empty and private")
            if data["phase"] == "publishing":
                _locations(journal, payload)
        return data["phase"], data["snapshot_id"]
    except (ValueError, TypeError):
        _fail("restore_config", "Restore free-space reserve is invalid")
    except OSError as error:
        _io_failure(error)
    finally:
        for handle in (payload, stage, fd):
            if handle is not None:
                os.close(handle)


def resume_snapshot(config) -> str | None:
    """Return the verified pending snapshot, excluding completed/abandoned work."""
    state = _resume_state(config)
    return state[1] if state and state[0] not in {"complete", "resetting"} else None


def resume_phase(config) -> str | None:
    """Return the verified phase so publication can resume without a repository.

    For publishing, core calls restore_to_volume with resume_snapshot's ID and a
    callback that must never execute. Complete likewise requires no repository;
    restore_to_volume will skip (and finish any private staging cleanup).
    """
    state = _resume_state(config)
    return state[0] if state else None


def reset_failed_population(config, snapshot_id: str) -> bool:
    """Explicitly abandon only verified, not-yet-published candidate staging.

    Intended for a verified checksum/manifest failure before trying an older
    candidate. Never use this to bypass a DB/process, ownership or mount error.
    Publishing/completed volumes are refused. The durable resetting phase makes
    this operation restartable even between removing payload, stage and marker.
    """
    fd = stage = payload = None
    try:
        fd, journal, exists = _locked_journal(config)
        if not exists:
            return False
        data = journal.data
        if (data["snapshot_id"] != snapshot_id
                or data["phase"] not in {"allocating", "populating", "resetting"}
                or data["owned_entries"]):
            _fail("restore_conflict", "Published or unrelated restore data cannot be reset")
        _root_entries(journal)
        if data["phase"] == "resetting":
            _finish_reset(journal)
            return True
        stage = _stage(journal)
        payload = _payload(journal, stage)
        _inactive_database(payload, journal.mount)
        _walk(payload, journal.mount, {}, sync=False)
        data["phase"] = "resetting"
        journal.save(stage)
        os.close(payload)
        payload = None
        os.close(stage)
        stage = None
        _finish_reset(journal)
        return True
    except (ValueError, TypeError):
        _fail("restore_config", "Restore free-space reserve is invalid")
    except OSError as error:
        _io_failure(error)
    finally:
        for handle in (payload, stage, fd):
            if handle is not None:
                os.close(handle)


def _io_failure(error: OSError) -> None:
    if error.errno in {errno.ENOSPC, errno.EDQUOT}:
        _fail("restore_no_space", "Restore ran out of space; existing application data was preserved")
    if error.errno in {errno.ELOOP, errno.ENOTDIR}:
        _fail("restore_path", "Restore paths must be real directories without symlink components")
    if error.errno in {errno.EPERM, errno.EACCES}:
        _fail("restore_permissions", "Restore lacks required filesystem permissions or ownership capabilities")
    _fail("restore_io", "Restore filesystem operation failed; recovery journal was retained")


def restore_to_volume(config, snapshot_id: str, populate: Callable[[Path], None]) -> dict:
    """Restore an empty volume or resume this exact dataset/snapshot only.

    Results omit paths, filenames and IDs. FORCESYNC never grants authority over
    unrelated data. RESTORE_MIN_FREE_BYTES may increase the 1MiB metadata reserve.
    work_path is intentionally unused: staging MUST be on the destination mount.
    """
    target_fd = stage_fd = payload_fd = None
    try:
        dataset = config.dataset_id
        for value in (dataset, snapshot_id):
            if (not isinstance(value, str) or not value or len(value) > 512
                    or any(ord(char) < 32 for char in value)):
                _fail("restore_request", "Restore dataset and snapshot identifiers are invalid")
        path = Path(config.data_path)
        env = getattr(config, "env", {})
        try:
            reserve = int(env.get("RESTORE_MIN_FREE_BYTES", _MIN_FREE))
        except (ValueError, TypeError):
            _fail("restore_config", "Restore free-space reserve is invalid")
        if reserve < _MIN_FREE:
            _fail("restore_config", "Restore free-space reserve cannot be below 1MiB")
        target_fd = _target(path)
        try:
            fcntl.flock(target_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _fail("restore_busy", "Another restore owns this volume")
        identity = _identity(os.fstat(target_fd))
        journal = _Journal(target_fd, _mount_id(target_fd), identity, reserve)
        exists = journal.read()
        if exists and journal.data["dataset_id"] != dataset:
            _fail("restore_conflict", "Restore journal belongs to a different dataset")
        if exists and journal.data["phase"] == "resetting":
            _finish_reset(journal)
            exists = False
        if not exists:
            names = os.listdir(target_fd)
            if any(name.startswith(_PREFIX) for name in names):
                _fail("restore_marker", "Unowned restore metadata exists; existing entries were preserved")
            if names:
                return {"status": "skipped", "reason": "nonempty"}
            _space(target_fd, reserve)
            journal.create(dataset, snapshot_id)
        elif journal.data["phase"] == "complete":
            stage_fd = _stage(journal, missing_ok=True)
            _cleanup_complete(journal, stage_fd)
            return {"status": "skipped", "reason": "already_restored" if
                    journal.data["snapshot_id"] == snapshot_id else "nonempty"}
        elif journal.data["snapshot_id"] != snapshot_id:
            _fail("restore_conflict", "Interrupted restore belongs to a different snapshot")
        # Reclaim only verified partial staging before checking reserve again.
        # Otherwise an ENOSPC callback could permanently prevent its own retry.
        _root_entries(journal)
        stage_fd = _stage(journal)
        payload_fd = _payload(journal, stage_fd)
        if journal.data["phase"] == "allocating":
            journal.data["phase"] = "populating"
            journal.save(stage_fd)
        if journal.data["phase"] == "populating":
            _root_entries(journal)
            _reset_payload(journal, payload_fd)
            _space(target_fd, reserve)
            _same_target(path, identity, journal.mount)
            try:
                populate(path / journal.data["stage"] / _PAYLOAD)
            except (BackupError, OSError):
                raise
            except Exception:
                _fail("restore_populate_failed", "Restore population failed; only private staging may be retried")
            _same_target(path, identity, journal.mount)
            _owned(target_fd, journal.data["stage"], journal.data["stage_identity"], journal.mount)
            _owned(stage_fd, _PAYLOAD, journal.data["payload_identity"], journal.mount)
            stage_info = os.fstat(stage_fd)
            if (stage_info.st_uid != os.geteuid()
                    or stat.S_IMODE(stage_info.st_mode) not in {0o700, 0o710}):
                _fail("restore_ownership", "Restore callback changed private staging ownership")
            os.fchmod(stage_fd, 0o700)
            _root_entries(journal)
            _inactive_database(payload_fd, journal.mount)
            links = {}
            _walk(payload_fd, journal.mount, links)
            _links_safe(links)
            info = os.fstat(payload_fd)
            metadata = {"uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode)}
            if os.geteuid() != 0 and (info.st_uid != os.geteuid() or metadata["mode"] & 0o700 != 0o700):
                _fail("restore_permissions", "Final volume permissions would prevent durable publication")
            journal.data["root_metadata"] = metadata
            journal.data["owned_entries"] = [
                {"name": name, "identity": _identity(_describe(payload_fd, name, journal.mount)),
                 "published": False} for name in sorted(os.listdir(payload_fd))
            ]
            journal.data["phase"] = "publishing"
            journal.save(stage_fd)
        _publish(journal, stage_fd, payload_fd)
        os.close(payload_fd)
        payload_fd = None
        _cleanup_complete(journal, stage_fd)
        return {"status": "restored"}
    except OSError as error:
        _io_failure(error)
    finally:
        for fd in (payload_fd, stage_fd, target_fd):
            if fd is not None:
                os.close(fd)
