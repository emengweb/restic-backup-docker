"""Compatible configuration. Secrets stay private and are never included in repr."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import os
import re
import shlex
from urllib.parse import urlsplit, urlunsplit
from .errors import BackupError


def boolean(value: str | None, default: bool = False) -> bool:
    if value is None or value == "": return default
    if value.lower() in {"true", "1", "yes", "on"}: return True
    if value.lower() in {"false", "0", "no", "off"}: return False
    raise BackupError("invalid_config", "Boolean configuration is invalid")


def integer(env: dict, key: str, default: int, minimum: int = 1) -> int:
    try: value = int(env.get(key, default))
    except (TypeError, ValueError): raise BackupError("invalid_config", "Numeric configuration is invalid") from None
    if value < minimum: raise BackupError("invalid_config", "Numeric configuration is below its minimum")
    return value


def read_secret(env: dict, key: str) -> str:
    filename = env.get(key + "_FILE")
    if filename:
        if env.get(key): raise BackupError("ambiguous_secret", "Both secret value and secret file were configured")
        try:
            with open(filename, encoding="utf-8") as f: return f.read(1024 * 1024).rstrip("\r\n")
        except OSError: raise BackupError("secret_file_unreadable", "A configured secret file is not readable") from None
    return env.get(key, "")


def repo_identity(repo: str) -> str:
    # Credentials/query fragments must not affect data identity, and never enter the tag.
    raw = repo
    for prefix in ["s3:", "rest:", "azure:", "gs:"]:
        if raw.startswith(prefix) and "://" in raw[len(prefix):]:
            raw = raw[len(prefix):]; break
    u = urlsplit(raw)
    if u.scheme and u.netloc:
        raw = urlunsplit((u.scheme, (u.hostname or "") + (":" + str(u.port) if u.port else ""), u.path.rstrip("/"), "", ""))
    return "repo-" + hashlib.sha256(raw.encode()).hexdigest()[:24]


@dataclass(repr=False)
class Config:
    env: dict = field(repr=False)
    engine: str
    dataset_id: str
    data_path: Path
    work_path: Path
    databases: list[str]
    db_host: str
    db_port: str
    db_user: str
    db_password: str = field(repr=False)
    target_major: str | None
    run_timeout: int
    repository: str = field(repr=False)
    restic_bin: str
    independent_repo: bool
    max_bytes: int
    min_free_bytes: int
    fallback_candidates: int
    max_restore_age: int
    backup_cron: str
    check_cron: str
    prune_cron: str
    backup_window: str
    timezone: str

    @property
    def kind(self):
        mode = self.env.get("BACKUP_MODE", "logical" if self.engine != "files" else "files")
        if mode == "physical" and self.engine != "files": return "pg-physical" if self.engine == "postgres" else "mariadb-physical"
        return {"files": "files", "postgres": "pg-logical", "mariadb": "mariadb-logical"}[self.engine]

    @classmethod
    def from_env(cls, env=None):
        e = dict(os.environ if env is None else env)
        aliases = {"": "files", "files": "files", "file": "files", "pg": "postgres", "postgres": "postgres", "postgresql": "postgres", "mysql": "mariadb", "mariadb": "mariadb"}
        raw = e.get("DATABASE_TYPE", e.get("BACKUP_KIND", "")).lower()
        if raw not in aliases: raise BackupError("unsupported_engine", "Database engine is not implemented")
        engine = aliases[raw]
        if e.get("BACKUP_MODE", "logical" if engine != "files" else "files") not in {"logical", "physical", "dual", "files"}:
            raise BackupError("invalid_backup_mode", "Unknown backup mode")
        repo = e.get("RESTIC_REPOSITORY", "")
        if not repo: raise BackupError("repository_required", "RESTIC_REPOSITORY is required")
        independent = boolean(e.get("REPOSITORY_ISOLATED"), True)
        dataset = e.get("DATASET_ID", "")
        if not independent and not dataset: raise BackupError("dataset_required", "A shared repository requires a stable DATASET_ID")
        dataset = dataset or repo_identity(repo)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,199}", dataset):
            raise BackupError("invalid_dataset", "Data identity contains unsupported characters")
        def first(*keys, default=""):
            return next((e[k] for k in keys if e.get(k)), default)
        prefix = "PG" if engine == "postgres" else "MYSQL"
        user = first("DATABASE_USER", prefix + "_USER", "POSTGRES_USER", "MARIADB_USER")
        db = first("DATABASE_NAME", prefix + "_DATABASE", prefix + "_DB", "POSTGRES_DB", "MYSQL_DATABASE", "MARIADB_DATABASE")
        databases = [x.strip() for x in db.split(",") if x.strip()]
        if engine != "files" and (not user or not databases):
            raise BackupError("database_config_required", "Database user and explicit database list are required")
        password = ""
        for k in ["DATABASE_PASSWORD", prefix + "_PASSWORD", "POSTGRES_PASSWORD", "MARIADB_PASSWORD", "MARIADB_ROOT_PASSWORD", "MYSQL_ROOT_PASSWORD"]:
            if e.get(k) or e.get(k + "_FILE"):
                password = read_secret(e, k); break
        if e.get("RESTIC_PASSWORD_FILE"):
            if e.get("RESTIC_PASSWORD"): raise BackupError("ambiguous_secret", "Both restic password value and file were configured")
        # Child tools get secret values in environment only, never arguments/manifest/log.
        for k in ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"]:
            if e.get(k + "_FILE"): e[k] = read_secret(e, k)
        data = Path(e.get("RECOVERY_DATA_PATH", e.get("DATA_PATH", "/data")))
        work = Path(e.get("BACKUP_WORK_PATH", "/backup"))
        if not data.is_absolute() or not work.is_absolute() or data == Path("/") or work == Path("/"):
            raise BackupError("invalid_path", "Data and work directories must be separate absolute non-root paths")
        dr, wr = data.resolve(), work.resolve()
        if dr == wr or dr in wr.parents or wr in dr.parents:
            raise BackupError("overlapping_paths", "Work and data directories must not contain one another")
        major = first("RESTORE_TARGET_MAJOR", "TARGET_DATABASE_MAJOR", "DATABASE_MAJOR", "PG_MAJOR", default="") or None
        from .profiles import check_engine
        check_engine(engine, major)
        window = e.get("DATABASE_BACKUP_TIME", "0-23")
        if not re.fullmatch(r"(?:[01]?\d|2[0-3])-(?:[01]?\d|2[0-3])", window):
            raise BackupError("invalid_window", "Database backup window must be HH-HH (0..23)")
        tz = e.get("TZ", "Asia/Shanghai")
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try: ZoneInfo(tz)
        except ZoneInfoNotFoundError: raise BackupError("invalid_timezone", "Unknown timezone") from None
        return cls(e, engine, dataset, data, work, databases,
                   first("DATABASE_HOST", prefix + "_HOST", "POSTGRES_HOST" if engine == "postgres" else "MARIADB_HOST", default="127.0.0.1"),
                   first("DATABASE_PORT", prefix + "_PORT", "POSTGRES_PORT" if engine == "postgres" else "MARIADB_PORT", default="5432" if engine == "postgres" else "3306"),
                   user, password, major, integer(e, "JOB_TIMEOUT_SECONDS", 900), repo,
                   e.get("RESTIC_BIN", "restic"), independent,
                   integer(e, "BACKUP_MAX_BYTES", 2 * 1024**3),
                   integer(e, "BACKUP_MIN_FREE_BYTES", 256 * 1024**2),
                   integer(e, "RESTORE_MAX_CANDIDATES", 5),
                   integer(e, "RESTORE_MAX_AGE_SECONDS", 7 * 86400, minimum=0),
                   e.get("BACKUP_CRON", "0 */6 * * *"), e.get("CHECK_CRON", ""),
                   e.get("PRUNE_CRON", ""), window, tz)
