"""Restic backend integration. No Pod or node identity participates in selection."""
from __future__ import annotations
from pathlib import Path, PurePosixPath
from datetime import datetime, timezone
import json
import os
import re
import shlex
from .config import boolean
from .errors import BackupError
from .process import run


def timestamp(value):
    try: return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError): raise BackupError("invalid_snapshot", "Invalid snapshot timestamp") from None


class Repository:
    def __init__(self, config): self.config = config

    def command(self, args, *, check=True, timeout=None):
        r = run([self.config.restic_bin, "--no-cache", "--retry-lock", "30s", *args], env=self.config.env,
                timeout=timeout or self.config.run_timeout, check=False)
        if check and r.returncode:
            raw = r.stderr.lower()
            if b"locked" in raw or b"lock" in raw and b"unable" in raw: code = "repository_locked"
            elif any(x in raw for x in [b"wrong password", b"no key found", b"accessdenied", b"access denied", b"forbidden"]): code = "repository_auth_failed"
            elif b"x509" in raw or b"certificate" in raw: code = "repository_tls_failed"
            elif any(x in raw for x in [b"no space left", b"permission denied", b"read-only file system", b"input/output error", b"connection reset", b"timed out", b"timeout", b"connection refused"]): code = "repository_failed"
            elif args and args[0] == "restore" and any(x in raw for x in [b"ciphertext verification failed", b"checksum mismatch", b"blob not found"]): code = "snapshot_corrupt"
            else: code = "repository_failed"
            raise BackupError(code, "Restic command failed; repository was not initialized, unlocked or pruned")
        return r

    def probe(self):
        # Lack of a repository is an error, not implicit permission to initialize one.
        r = self.command(["cat", "config"], timeout=30)
        try: d = json.loads(r.stdout)
        except ValueError: raise BackupError("invalid_repository", "Repository config is invalid") from None
        if d.get("version") not in [1, 2]: raise BackupError("unsupported_repository", "Repository version is not supported")

    def snapshots(self):
        r = self.command(["snapshots", "--json"])
        try: data = json.loads(r.stdout) or []
        except ValueError: raise BackupError("invalid_snapshot_list", "Snapshot metadata is invalid") from None
        if not isinstance(data, list): raise BackupError("invalid_snapshot_list", "Unexpected snapshot metadata")
        for x in data:
            if not isinstance(x, dict) or not re.fullmatch(r"[0-9a-f]{64}", x.get("id", "")):
                raise BackupError("invalid_snapshot_list", "Invalid snapshot identity")
        return data

    def candidates(self, resume_id=None):
        c = self.config
        selected = []
        legacy = boolean(c.env.get("RESTORE_LEGACY"), c.independent_repo)
        physical = boolean(c.env.get("RESTORE_LEGACY_PHYSICAL"), False)
        for s in self.snapshots():
            tags = set(s.get("tags") or [])
            scoped = "dataset=" + c.dataset_id in tags
            kinds = {"files"} if c.engine == "files" else {"pg-logical", "pg-physical"} if c.engine == "postgres" else {"mariadb-logical", "mariadb-physical"}
            new = scoped and "schema=1" in tags and any("kind=" + k in tags for k in kinds)
            # No downgrade to a different dataset's legacy stream, even in isolated mode.
            foreign = any(t.startswith("dataset=") and t != "dataset=" + c.dataset_id for t in tags)
            is_legacy = not foreign and legacy and "/data" in (s.get("paths") or []) and not any(t.startswith("schema=") for t in tags)
            legacy_paths = c.env.get("LEGACY_NATIVE_PATHS", "/backup/export/current").split(",")
            native_affine = (c.engine == "postgres" and c.independent_repo and legacy and not foreign
                             and any(p in (s.get("paths") or []) for p in legacy_paths) and not any(t.startswith("schema=") for t in tags))
            if new: mode = "manifest"
            elif native_affine: mode = "legacy-affine-native"
            elif is_legacy and (c.engine == "files" or physical): mode = "legacy-files" if c.engine == "files" else "legacy-physical"
            else: continue
            age = (datetime.now(timezone.utc) - timestamp(s["time"])).total_seconds()
            if age < -300: continue
            if c.max_restore_age and age > c.max_restore_age and s["id"] != resume_id: continue
            selected.append(dict(s, _mode=mode))
        selected.sort(key=lambda s: (timestamp(s["time"]), s["id"]), reverse=True)
        if resume_id:
            pending = next((s for s in selected if s["id"] == resume_id), None)
            if pending: return [pending] + [s for s in selected if s["id"] != resume_id][:c.fallback_candidates]
        return selected[:c.fallback_candidates]

    def backup(self, path: Path, source: Path | None = None):
        c = self.config
        args = ["backup", *([str(source)] if source else []), str(path), "--json", "--tag", "schema=1", "--tag", "dataset=" + c.dataset_id, "--tag", "kind=" + c.kind]
        if source:
            args += ["--exclude", ".k3s-restic-*", "--exclude", "*/.k3s-restic-*"]
            args += self.file_args()
        # Stable dataset display-host avoids retention fragmentation, but restore NEVER filters it.
        args += ["--host", c.dataset_id.replace("/", "-")]
        if c.env.get("RESTIC_TAG"): args += ["--tag", c.env["RESTIC_TAG"]]
        r = self.command(args)
        summary = None
        for line in r.stdout.splitlines():
            try: d = json.loads(line)
            except ValueError: continue
            if d.get("message_type") == "summary": summary = d
        ident = (summary or {}).get("snapshot_id", "")
        if not re.fullmatch(r"[0-9a-f]{8,64}", ident):
            raise BackupError("snapshot_not_confirmed", "Restic did not confirm a complete snapshot")
        return ident

    def file_args(self):
        try: args = shlex.split(self.config.env.get("RESTIC_JOB_ARGS", ""))
        except ValueError: raise BackupError("invalid_backup_args", "File backup flags are invalid") from None
        allowed = {"--exclude", "--exclude-file", "--exclude-if-present", "--iexclude", "--iexclude-file"}
        switches = {"--one-file-system", "--exclude-caches"}
        out = []; i = 0
        while i < len(args):
            key, sep, value = args[i].partition("=")
            if key in switches and not sep: out.append(key)
            elif key in allowed:
                if not sep:
                    i += 1
                    if i >= len(args): raise BackupError("invalid_backup_args", "Backup flag requires a value")
                    value = args[i]
                out += [key, value]
            else: raise BackupError("unsafe_backup_args", "Unrecognized file backup option requires review")
            i += 1
        return out

    def restore(self, snapshot, target: Path):
        # Preflight the entire restored footprint before writing a single payload byte.
        r = self.command(["stats", snapshot["id"], "--mode", "restore-size", "--json"])
        try: size = int(json.loads(r.stdout)["total_size"])
        except (ValueError, KeyError, TypeError): raise BackupError("invalid_restore_size", "Restored size could not be established") from None
        if size < 0 or size > self.config.max_bytes:
            raise BackupError("size_limit", "Snapshot footprint exceeds the configured restore budget")
        import shutil
        free = shutil.disk_usage(target).free
        # Scratch plus same-filesystem staged publication can require two copies.
        if free < size * 2 + self.config.min_free_bytes:
            raise BackupError("insufficient_space", "Snapshot scratch and publication headroom are insufficient")
        # Pin automatic latest selection to one exact snapshot; host never used.
        self.command(["restore", snapshot["id"], "--target", str(target), "--verify"])

    def envelope(self, snapshot, target: Path):
        paths = snapshot.get("paths") or []
        if len(paths) == 2 and "kind=files" in (snapshot.get("tags") or []):
            roots = [target.joinpath(*PurePosixPath(x).parts[1:]) for x in paths if isinstance(x, str) and PurePosixPath(x).is_absolute() and ".." not in PurePosixPath(x).parts]
            matches = []
            for x in roots:
                p = x / "backup-manifest.json"
                if not p.is_file() or p.is_symlink() or p.stat().st_size > 8 * 1024 * 1024: continue
                try: md = json.loads(p.read_text())
                except (ValueError, OSError): continue
                if md.get("schema_version") == 1 and md.get("complete") is True and md.get("kind") == "files" and md.get("dataset_id") == self.config.dataset_id and md.get("source_path") in paths and md.get("source_path") != "/" + x.relative_to(target).as_posix(): matches.append(x)
            if len(matches) != 1: raise BackupError("ambiguous_snapshot", "File backup manifest root is ambiguous")
            p = PurePosixPath("/" + matches[0].relative_to(target).as_posix())
        elif len(paths) == 1:
            p = PurePosixPath(paths[0])
        else:
            raise BackupError("ambiguous_snapshot", "Snapshot roots require a declared format adapter")
        if not p.is_absolute() or ".." in p.parts:
            raise BackupError("unsafe_snapshot_path", "Snapshot root path is invalid")
        root = target.joinpath(*p.parts[1:])
        if root.is_symlink() or not root.is_dir() or target.resolve() not in root.resolve().parents:
            raise BackupError("unsafe_snapshot_path", "Snapshot root escapes its restore scratch")
        return root

    def check(self):
        c = self.config
        args = ["check", "--json"]
        if c.env.get("RESTIC_DATA_SUBSET"): args += ["--read-data-subset", c.env["RESTIC_DATA_SUBSET"]]
        if boolean(c.env.get("CHECK_READ_ALL_DATA")): args = ["check", "--json", "--read-data"]
        r = self.command(args, check=False)
        if r.returncode == 0: return {"status": "checked", "scope": "whole_repository", "data": "all" if "--read-data" in args else "subset" if "--read-data-subset" in args else "metadata"}
        summary = {}
        for line in r.stdout.splitlines():
            try: d = json.loads(line)
            except ValueError: continue
            if isinstance(d, dict) and ("suggest_repair_index" in d or d.get("message_type") == "summary"): summary = d
        repair = boolean(c.env.get("AUTO_REPAIR_INDEX"), False)
        # Extra gate required: native locks serialize commands, not the entire repair/recheck sequence.
        excluded = boolean(c.env.get("MAINTENANCE_EXCLUSIVE"), False)
        rollback = boolean(c.env.get("REPAIR_ROLLBACK_AVAILABLE"), False)
        if repair and excluded and rollback and summary.get("suggest_repair_index") and not summary.get("broken_packs"):
            self.command(["repair", "index"])
            verify = self.command(args, check=False)
            if verify.returncode == 0: return {"status": "index_rebuilt", "scope": "whole_repository", "data_reconstructed": False}
        raise BackupError("repository_check_failed", "Whole-repository check failed; no data-loss repair or pruning was attempted")

    def prune(self, *, apply=False):
        c = self.config
        raw = c.env.get("RESTIC_FORGET_ARGS", "")
        try: args = shlex.split(raw)
        except ValueError: raise BackupError("invalid_retention", "Retention policy could not be parsed") from None
        allowed = {"--keep-last", "--keep-hourly", "--keep-daily", "--keep-weekly", "--keep-monthly", "--keep-yearly", "--keep-within", "--keep-within-hourly", "--keep-within-daily", "--keep-within-weekly", "--keep-within-monthly", "--keep-within-yearly"}
        clean = []; i = 0
        while i < len(args):
            a = args[i]
            if a == "--prune": i += 1; continue
            key, sep, value = a.partition("=")
            if key not in allowed: raise BackupError("unsafe_retention", "Retention flags outside the allowlist are prohibited")
            if not sep:
                i += 1
                if i >= len(args): raise BackupError("invalid_retention", "Retention option requires a value")
                value = args[i]
            if not re.fullmatch(r"[0-9][0-9yMwdhms]*", value): raise BackupError("invalid_retention", "Invalid retention value")
            if key == "--keep-last" and int(value) < 1:
                raise BackupError("unsafe_retention", "At least one recovery point per stream must be kept")
            if key == "--keep-within":
                parts = re.findall(r"(\d+)([yMwdhms])", value)
                factors = {"y":31536000,"M":2592000,"w":604800,"d":86400,"h":3600,"m":60,"s":1}
                if not parts or "".join(n+u for n,u in parts) != value:
                    raise BackupError("invalid_retention", "Keep-within duration is invalid")
                if sum(int(n)*factors[u] for n,u in parts) < 7*86400: value = "7d"
            clean += [key, value]; i += 1
        if not clean: raise BackupError("retention_required", "A keep policy is required before maintenance")
        if apply:
            if not boolean(c.env.get("MAINTENANCE_EXCLUSIVE")) or not boolean(c.env.get("RETENTION_APPLY")):
                raise BackupError("maintenance_gate", "Prune application requires explicit exclusive-maintenance policy")
            # Persisted operator/controller evidence, never a container-age or boolean claim.
            proof_path = c.env.get("RETENTION_PROOF_FILE", "")
            if not proof_path: raise BackupError("observation_gate", "Persisted restore and observation evidence is required")
            try:
                p = Path(proof_path)
                if p.is_symlink() or not p.is_file() or p.stat().st_size > 1024 * 1024: raise ValueError()
                proof = json.loads(p.read_text())
            except (ValueError, OSError): raise BackupError("invalid_observation_proof", "Observation evidence is invalid") from None
            if proof.get("schema_version") != 1 or proof.get("repository_identity") != c.dataset_id:
                raise BackupError("observation_gate", "Observation evidence does not match this repository")
            now = datetime.now(timezone.utc)
            start = timestamp(proof.get("stable_since", ""))
            end = timestamp(proof.get("observed_until", ""))
            if (end - start).total_seconds() < 7 * 86400 or not 0 <= (now - end).total_seconds() <= 86400 or proof.get("unresolved_failures") != 0:
                raise BackupError("observation_gate", "Seven stable days and a recent observation with no unresolved failures are required")
            snapshots = {s["id"]: s for s in self.snapshots()}
            verified = proof.get("restore_verified", [])
            if not verified or any(not isinstance(x, dict) or x.get("snapshot_id") not in snapshots or not x.get("verified_at") or (now-timestamp(x["verified_at"])).total_seconds() > 86400 for x in verified):
                raise BackupError("restore_verification_required", "Existing recent verified recovery points are required before cleanup")
            # Protect verified points in every stream by ensuring the newest snapshot was restored.
            for x in verified:
                s = snapshots[x["snapshot_id"]]
                same = [v for v in snapshots.values() if v.get("paths") == s.get("paths") and v.get("tags") == s.get("tags")]
                if s["id"] != max(same, key=lambda v: timestamp(v["time"]))["id"]:
                    raise BackupError("restore_verification_required", "Restore proof must cover the newest snapshot in its stream")
        self.check()
        # Keep each legacy/native path and complete tag stream independently; ignore changing host.
        forget = ["forget", "--json", "--group-by", "paths,tags", "--keep-within", "7d", "--keep-last", "1", *clean]
        if not apply: forget += ["--dry-run"]
        self.command(forget)
        if apply:
            self.command(["prune"])
            self.check()
        return {"status": "pruned" if apply else "planned", "scope": "whole_repository", "destructive": apply}
