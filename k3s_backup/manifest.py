"""Versioned backup envelopes and path-safe complete verification."""
from __future__ import annotations
from pathlib import Path, PurePosixPath
import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from .errors import BackupError

NAME = "backup-manifest.json"
SCHEMA = 1


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024): h.update(chunk)
    return h.hexdigest()


def relative(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        raise BackupError("unsafe_artifact", "Archive has an unsafe path")
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or str(p) != value or value == ".":
        raise BackupError("unsafe_artifact", "Archive has an unsafe path")
    return p


def safe_symlink(root: Path, path: Path, value: str) -> None:
    if not value or os.path.isabs(value) or "\0" in value:
        raise BackupError("unsafe_symlink", "Archive symlink escapes its tree")
    target = (path.parent / value).resolve()
    if target != root.resolve() and root.resolve() not in target.parents:
        raise BackupError("unsafe_symlink", "Archive symlink escapes its tree")


def inspect(root: Path, limit: int) -> list[dict]:
    result, total = [], 0
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not d.startswith(".k3s-restic-")]
        files = [f for f in files if not f.startswith(".k3s-restic-")]
        for name in sorted(dirs + files):
            p = Path(folder) / name
            rel = p.relative_to(root).as_posix()
            if rel == NAME: continue
            mode = p.lstat().st_mode
            item = {"path": rel, "mode": stat.S_IMODE(mode), "uid": p.lstat().st_uid, "gid": p.lstat().st_gid}
            if stat.S_ISLNK(mode):
                link = os.readlink(p); safe_symlink(root, p, link)
                item.update(type="symlink", bytes=0, link_target=link)
            elif stat.S_ISREG(mode):
                size = p.stat().st_size; total += size
                if total > limit: raise BackupError("size_limit", "Backup exceeds configured capacity")
                item.update(type="file", bytes=size, sha256=sha256(p))
            elif stat.S_ISDIR(mode): item.update(type="dir", bytes=0)
            else: raise BackupError("unsupported_artifact", "Device, socket or special file in backup")
            result.append(item)
    return sorted(result, key=lambda x: x["path"])


def create(root: Path, config, engine: dict, payload_root: Path | None = None) -> dict:
    data = {"schema_version": SCHEMA, "dataset_id": config.dataset_id, "kind": config.kind,
            "created_at": datetime.now(timezone.utc).isoformat(), "engine": engine,
            "complete": True, "source_path": str(config.data_path),
            "artifact_integrity": "restic-tree" if payload_root is not None else "sha256",
            "artifacts": [] if payload_root is not None else inspect(root, config.max_bytes)}
    p = root / NAME
    with p.open("x", encoding="utf-8") as f:
        os.chmod(p, 0o600); json.dump(data, f, separators=(",", ":")); f.flush(); os.fsync(f.fileno())
    return data


def verify(root: Path, config, payload_root: Path | None = None) -> dict:
    p = root / NAME
    if p.is_symlink() or not p.is_file() or p.stat().st_size > 8 * 1024 * 1024:
        raise BackupError("manifest_missing", "Complete backup manifest is missing or invalid")
    try: d = json.loads(p.read_text())
    except (ValueError, OSError): raise BackupError("invalid_manifest", "Backup manifest cannot be decoded") from None
    if d.get("schema_version") != SCHEMA or d.get("complete") is not True:
        raise BackupError("invalid_manifest", "Backup schema is not supported or incomplete")
    kinds = {"files"} if config.engine == "files" else {"pg-logical", "pg-physical"} if config.engine == "postgres" else {"mariadb-logical", "mariadb-physical"}
    if d.get("dataset_id") != config.dataset_id or d.get("kind") not in kinds:
        raise BackupError("dataset_mismatch", "Snapshot does not match this dataset and engine")
    artifacts = d.get("artifacts")
    if not isinstance(artifacts, list): raise BackupError("invalid_manifest", "Manifest artifacts are missing")
    expected = set()
    for a in artifacts:
        if not isinstance(a, dict): raise BackupError("invalid_manifest", "Invalid artifact metadata")
        rel = str(relative(a.get("path"))); expected.add(rel)
    if len(expected) != len(artifacts): raise BackupError("duplicate_artifact", "Duplicate manifest paths")
    if payload_root is not None and d.get("artifact_integrity") == "restic-tree":
        # Restic restore --verify authenticates the full file tree, including source excludes.
        # Do not duplicate hashes or require a whole-volume copy to sidecar scratch.
        # Exclude rules were applied by Restic; only the restored tree is validated.
        inspect(payload_root, config.max_bytes)
        return d
    actual = inspect(payload_root or root, config.max_bytes)
    if actual != artifacts:
        # Mode/UID ownership is part of the restic tree and must be restored, not just bytes.
        raise BackupError("artifact_mismatch", "Backup files, ownership or checksums do not match manifest")
    return d
