"""Native database adapters; no shell, live-directory copy or success-on-error.

Only encrypted snapshot payloads contain database names, owners and grants.  No
command output or connection metadata is logged here.  PostgreSQL dumps are
individually consistent (not a cross-database distributed snapshot). MariaDB
uses one InnoDB transaction for the declared database set. Both require callers
to quiesce concurrent DDL; roles/globals are a separate capture.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import signal
import stat
import stringprep
import subprocess
import tempfile
import time
import warnings
import unicodedata

from .errors import BackupError
from .process import run
from .profiles import pg_probe_major, check_engine

PG_MAJORS = {"16", "17"}
MARIA_SOURCES = {"10.11", "11.4", "11.5", "11.8"}
_BASE_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"}


def _fail(code, message):
    raise BackupError(code, message)


def _env(config):
    return getattr(config, "env", {})


def _enabled(config, name):
    return str(_env(config).get(name, "false")).lower() in {"true", "1", "yes", "on"}


def _binary(config, key, default):
    value = str(_env(config).get(key, default))
    if not Path(value).is_absolute() or "\0" in value:
        _fail("invalid_binary", "Database executable paths must be absolute")
    return value


def _pg_bin(config, major, name):
    root = _env(config).get("PG_BIN_DIR_" + major, _env(config).get("PG_BIN_DIR", "/usr/lib/postgresql/" + major + "/bin"))
    return _binary(config, "PG_" + name.upper() + "_BIN", str(Path(root) / name))


def _maria_bin(config, name):
    defaults = {"client": "/usr/bin/mariadb", "dump": "/usr/bin/mariadb-dump",
                "server": "/usr/sbin/mariadbd", "install": "/usr/bin/mariadb-install-db",
                "admin": "/usr/bin/mariadb-admin"}
    return _binary(config, "MARIADB_" + name.upper() + "_BIN", defaults[name])


def _call(config, argv, **kwargs):
    kwargs.setdefault("env", dict(_BASE_ENV))
    kwargs.setdefault("timeout", config.run_timeout)
    result = run([str(a) for a in argv], check=True, **kwargs)
    # Also fail closed for alternate runners that do not implement check.
    if result.returncode:
        _fail("database_command_failed", "A native database command failed")
    return result


def _json_output(result):
    try:
        return json.loads(result.stdout.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError):
        _fail("invalid_database_metadata", "Native database metadata could not be decoded")


def _names(config):
    names = list(config.databases)
    if not names or len(names) != len(set(names)):
        _fail("invalid_databases", "An explicit, unique database list is required")
    if any(not isinstance(n, str) or not n or "\0" in n or any(ord(c) < 32 for c in n) for n in names):
        _fail("invalid_databases", "A database name contains unsupported characters")
    system = {"template0", "template1"} if config.engine == "postgres" else {"mysql", "sys", "information_schema", "performance_schema"}
    if set(names) & system:
        _fail("system_database", "System databases must be captured through native globals export")
    return names


def _directory(path, *, empty=False):
    path = Path(path).absolute()
    try:
        info = path.lstat()
    except OSError:
        _fail("invalid_database_path", "A database staging directory is unavailable")
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        _fail("invalid_database_path", "Database staging must be a real directory")
    if empty and any(path.iterdir()):
        _fail("database_target_not_empty", "Database staging must be empty")
    return path


def _archive(stage, name):
    if not isinstance(name, str) or not name or Path(name).name != name or name in {".", ".."}:
        _fail("invalid_database_archive", "A database archive path is unsafe")
    p = stage / name
    try:
        s = p.lstat()
    except OSError:
        _fail("missing_database_archive", "A required database archive is missing")
    if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or s.st_size == 0:
        _fail("invalid_database_archive", "Database archives must be nonempty regular files")
    return p


def _fsync_tree(path):
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            p = Path(root) / name
            info = p.lstat()
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                _fail("unsafe_database_tree", "Database output contains an unsupported filesystem entry")
            fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_private(path, content, uid=None, gid=None, *, replace=False):
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_TRUNC if replace else os.O_EXCL)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        if uid is not None and os.geteuid() == 0:
            os.fchown(fd, uid, gid)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(content.encode("utf-8") if isinstance(content, str) else content)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def _identity(config, engine):
    env = _env(config)
    prefix, account = ("POSTGRES", "postgres") if engine == "postgres" else ("MYSQL", "mysql")
    try:
        explicit = (env.get("DB_UID") or env.get(prefix + "_UID")) and (env.get("DB_GID") or env.get(prefix + "_GID"))
        default = pwd.getpwnam(account) if os.geteuid() == 0 and not explicit else None
        uid = int(env.get("DB_UID", env.get(prefix + "_UID", default.pw_uid if default else os.geteuid())))
        gid = int(env.get("DB_GID", env.get(prefix + "_GID", default.pw_gid if default else os.getegid())))
    except (KeyError, ValueError, TypeError):
        _fail("database_identity", "A dedicated database UID and GID must be configured")
    if uid <= 0 or gid <= 0 or (os.geteuid() != 0 and (uid != os.geteuid() or gid != os.getegid())):
        _fail("database_identity", "Database servers require a dedicated non-root UID and GID")
    return uid, gid


def _pg_restore_identities(config):
    """PG initdb needs an NSS identity; the final numeric owner does not."""
    try:
        if os.geteuid() == 0:
            account = pwd.getpwnam("postgres")
            helper = (account.pw_uid, account.pw_gid)
        else:
            account = pwd.getpwuid(os.geteuid())
            helper = (os.geteuid(), os.getegid())
    except KeyError:
        _fail("database_identity", "The PostgreSQL helper requires a resolvable non-root OS account")
    if not account.pw_name or any(type(n) is not int or n <= 0 or n >= 2**32 - 1 for n in helper):
        _fail("database_identity", "The PostgreSQL helper account has an invalid UID or GID")
    env = _env(config)
    requested = []
    for generic, alias, default in (("DB_UID", "POSTGRES_UID", helper[0]), ("DB_GID", "POSTGRES_GID", helper[1])):
        raw = env.get(generic, env.get(alias, str(default)))
        if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,10}", raw):
            _fail("database_identity", "The requested database UID and GID must be positive numeric IDs")
        value = int(raw)
        if value <= 0 or value >= 2**32 - 1:
            _fail("database_identity", "The requested database UID or GID is outside its supported range")
        requested.append(value)
    final = tuple(requested)
    if os.geteuid() != 0 and final != helper:
        _fail("database_identity", "A non-root helper cannot hand database files to a different UID or GID")
    return helper, final


def _as_database(config, argv, identity, *, parent_death=False):
    prefix = [_binary(config, "SETPRIV_BIN", "/usr/bin/setpriv")] if os.geteuid() == 0 or parent_death else []
    if os.geteuid() == 0:
        prefix += ["--reuid=" + str(identity[0]), "--regid=" + str(identity[1]), "--clear-groups"]
    if parent_death:
        prefix += ["--pdeathsig=TERM"]
    return prefix + (["--"] if prefix else []) + list(argv)


@contextmanager
def _database_access(config, target, engine, *, identity=None):
    """Grant group traversal only inside the restore module's owned stage."""
    identity = _identity(config, engine) if identity is None else identity
    parent = target.parent
    originals = []
    owned = re.fullmatch(r"\.k3s-restic-stage-[a-f0-9]{32}", parent.name)
    try:
        if owned:
            s = parent.lstat()
            if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.geteuid() or stat.S_IMODE(s.st_mode) != 0o700:
                _fail("unsafe_restore_stage", "The restore staging owner or mode is invalid")
            # A mount root may be root:root 0700, above the private stage.
            # Do not change its owner, inode or grant read/write to another UID.
            for path in (parent.parent, parent):
                before = path.lstat()
                if not stat.S_ISDIR(before.st_mode):
                    _fail("unsafe_restore_stage", "The restore staging ancestor is invalid")
                originals.append((path, before))
                if os.geteuid() == 0:
                    os.chown(path, -1, identity[1], follow_symlinks=False)
                os.chmod(path, stat.S_IMODE(before.st_mode) | 0o010, follow_symlinks=False)
        if os.geteuid() == 0:
            os.chown(target, *identity, follow_symlinks=False)
        os.chmod(target, 0o700, follow_symlinks=False)
        yield identity
    finally:
        for path, before in reversed(originals):
            now = path.lstat()
            if (now.st_dev, now.st_ino) != (before.st_dev, before.st_ino):
                _fail("unsafe_restore_stage", "The restore staging ancestor changed identity")
            os.chmod(path, stat.S_IMODE(before.st_mode), follow_symlinks=False)
            if os.geteuid() == 0:
                os.chown(path, -1, before.st_gid, follow_symlinks=False)

@contextmanager
def _runtime_dir(identity):
    # Short paths avoid the 107-byte Unix socket path limit on nested mounts.
    path = Path(tempfile.mkdtemp(prefix="k3s-db-", dir="/tmp"))
    try:
        if os.geteuid() == 0:
            os.chown(path, *identity)
        os.chmod(path, 0o700)
        yield path
    finally:
        # This randomly-created, never-published directory is our only rmtree.
        shutil.rmtree(path)


def _handoff_mount_id(fd):
    try:
        with open("/proc/self/fdinfo/" + str(fd), encoding="ascii") as stream:
            for line in stream:
                if line.startswith("mnt_id:"):
                    return int(line.split(":", 1)[1])
    except (OSError, ValueError):
        pass
    _fail("database_handoff_mount", "Cannot verify database handoff mount boundaries")


def _handoff_open_target(target):
    path = Path(target)
    if not path.is_absolute() or ".." in path.parts:
        _fail("database_handoff_path", "Database handoff target must be an absolute validated directory")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _handoff_database_owner(target, helper, final, expected, *, clean_shutdown):
    """Transfer only the pinned, newly-restored PG payload after clean shutdown.

    Preflight the entire tree before any chown. Every later open is relative to
    the pinned root, no-follow, rechecked by inode/mount/owner. No path deletion,
    shell, source archive, runtime socket, parent or journal is involved.
    """
    if not clean_shutdown:
        _fail("database_handoff_active", "Database ownership cannot change before verified clean shutdown")
    if helper == final:
        return
    if os.geteuid() != 0:
        _fail("database_identity", "Changing the final database UID or GID requires a root helper")
    root = None
    try:
        root = _handoff_open_target(target)
        initial = os.fstat(root)
        if (initial.st_dev, initial.st_ino) != expected:
            _fail("database_handoff_identity", "The database handoff target changed identity")
        mount = _handoff_mount_id(root)
        for marker in ("postmaster.pid", "server.pid"):
            try:
                os.stat(marker, dir_fd=root, follow_symlinks=False)
            except FileNotFoundError:
                continue
            _fail("database_handoff_active", "Database process markers must be absent before ownership handoff")
        records = []

        def validate(fd, relative):
            info = os.fstat(fd)
            if info.st_dev != initial.st_dev or _handoff_mount_id(fd) != mount:
                _fail("database_handoff_mount", "Database ownership handoff cannot cross a mount boundary")
            if (info.st_uid, info.st_gid) != helper:
                _fail("database_handoff_owner", "Database handoff found an unexpected filesystem owner")
            directory = stat.S_ISDIR(info.st_mode)
            if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                _fail("unsafe_database_tree", "Database handoff requires only unlinked regular files and real directories")
            if len(relative) > 64 or len(records) >= 100000:
                _fail("database_handoff_limit", "Database handoff exceeded the bounded tree traversal limit")
            records.append((relative, info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode), directory))
            if directory:
                names = os.listdir(fd)
                if len(names) > 100000 - len(records):
                    _fail("database_handoff_limit", "Database handoff exceeded the bounded entry limit")
                for name in names:
                    s = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if not (stat.S_ISDIR(s.st_mode) or stat.S_ISREG(s.st_mode)):
                        _fail("unsafe_database_tree", "Database handoff cannot follow links or special filesystem entries")
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                    if stat.S_ISDIR(s.st_mode):
                        flags |= os.O_DIRECTORY
                    child = os.open(name, flags, dir_fd=fd)
                    try:
                        current = os.fstat(child)
                        if (current.st_dev, current.st_ino) != (s.st_dev, s.st_ino):
                            _fail("database_handoff_identity", "A database handoff entry changed identity")
                        validate(child, relative + (name,))
                    finally:
                        os.close(child)

        validate(root, ())
        known = {relative: (dev, ino, mode, directory) for relative, dev, ino, mode, directory in records}
        # Children first; directories retain the helper owner until all descendants
        # have been processed. Open each chain anew without following symlinks.
        for relative, dev, ino, mode, directory in reversed(records):
            fd = os.dup(root)
            try:
                for depth, name in enumerate(relative):
                    parent_relative = relative[:depth]
                    parent_info = os.fstat(fd)
                    wanted = known[parent_relative]
                    if (parent_info.st_dev, parent_info.st_ino) != wanted[:2]:
                        _fail("database_handoff_identity", "A database handoff ancestor changed identity")
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                    if depth < len(relative) - 1 or directory:
                        flags |= os.O_DIRECTORY
                    child = os.open(name, flags, dir_fd=fd)
                    os.close(fd)
                    fd = child
                info = os.fstat(fd)
                if ((info.st_dev, info.st_ino) != (dev, ino) or _handoff_mount_id(fd) != mount or
                        (info.st_uid, info.st_gid) != helper or stat.S_IMODE(info.st_mode) != mode or
                        (not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1))):
                    _fail("database_handoff_identity", "A database handoff entry changed after validation")
                os.fchown(fd, *final)
                # Ordinary DB modes survive chown. Restore only changed bits,
                # avoiding an unnecessary CAP_FOWNER dependency after ownership
                # has moved away from root/helper (set-id modes may need it).
                if stat.S_IMODE(os.fstat(fd).st_mode) != mode:
                    os.fchmod(fd, mode)
                os.fsync(fd)
                after = os.fstat(fd)
                if (after.st_uid, after.st_gid) != final or stat.S_IMODE(after.st_mode) != mode:
                    _fail("database_handoff_owner", "Database ownership handoff did not preserve the requested metadata")
            finally:
                os.close(fd)
        # Reject newly-created entries after the complete preflight, not just a
        # replaced root pathname. The server is already stopped at this point.
        seen = set()
        def verify(fd, relative):
            if len(relative) > 64 or len(seen) >= 100000:
                _fail("database_handoff_limit", "Database handoff verification exceeded its bounded traversal limit")
            info = os.fstat(fd)
            wanted = known.get(relative)
            if (wanted is None or (info.st_dev, info.st_ino) != wanted[:2] or
                    (info.st_uid, info.st_gid) != final or _handoff_mount_id(fd) != mount or
                    stat.S_IMODE(info.st_mode) != wanted[2]):
                _fail("database_handoff_identity", "Database output changed during ownership handoff")
            seen.add(relative)
            if wanted[3]:
                if not stat.S_ISDIR(info.st_mode):
                    _fail("unsafe_database_tree", "A database handoff directory became a non-directory")
                names = os.listdir(fd)
                if len(names) > 100000 - len(seen):
                    _fail("database_handoff_limit", "Database handoff verification exceeded its bounded entry limit")
                for name in names:
                    child_relative = relative + (name,)
                    record = known.get(child_relative)
                    if record is None:
                        _fail("database_handoff_identity", "An unrecorded database output appeared during handoff")
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_DIRECTORY if record[3] else 0)
                    child = os.open(name, flags, dir_fd=fd)
                    try:
                        current = os.fstat(child)
                        if not record[3] and (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1):
                            _fail("unsafe_database_tree", "Database output became linked or special during ownership handoff")
                        verify(child, child_relative)
                    finally:
                        os.close(child)
                os.fsync(fd)
        verify(root, ())
        if len(seen) != len(records):
            _fail("database_handoff_identity", "Database output disappeared during ownership handoff")
        fd = _handoff_open_target(target)
        try:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != expected or _handoff_mount_id(fd) != mount:
                _fail("database_handoff_identity", "The published database target changed identity")
        finally:
            os.close(fd)
    except OSError:
        _fail("database_handoff_failed", "Database ownership handoff failed; no successful restore was recorded")
    finally:
        if root is not None:
            os.close(root)


def _pg_literal(value):
    if not isinstance(value, str) or "\0" in value:
        _fail("invalid_database_metadata", "Invalid database text metadata")
    return "'" + value.replace("'", "''") + "'"


def _pg_identifier(value):
    if not isinstance(value, str) or not value or "\0" in value:
        _fail("invalid_database_metadata", "Invalid database identifier metadata")
    return '"' + value.replace('"', '""') + '"'


def _pg_conn(config, db, *, socket=None, user=None):
    env = dict(_BASE_ENV)
    if socket is None:
        for k in ("PGSSLMODE", "PGSSLROOTCERT", "PGSSLCERT", "PGSSLKEY", "PGSSLCRL", "PGCHANNELBINDING"):
            if k in _env(config):
                env[k] = str(_env(config)[k])
        env.update(PGHOST=str(config.db_host), PGPORT=str(config.db_port), PGUSER=str(config.db_user), PGPASSWORD=config.db_password)
    else:
        env.update(PGHOST=str(socket), PGPORT="5432", PGUSER=user, PGPASSWORD="", PGSSLMODE="disable")
    # libpq treats bare database strings containing '=' as connection strings.
    escaped = db.replace("\\", "\\\\").replace("'", "\\'")
    env.update(PGDATABASE="dbname='" + escaped + "'", PGCONNECT_TIMEOUT=str(min(config.run_timeout, 15)), PGAPPNAME="k3s-backup")
    return env


def _pg_query(config, major, sql, env):
    return _call(config, [_pg_bin(config, major, "psql"), "-X", "-qAt", "--no-password", "--dbname=" + env["PGDATABASE"], "-v", "ON_ERROR_STOP=1", "-f", "-"],
                 env=env, input_data=("SET standard_conforming_strings=on;\n" + sql + "\n").encode())


def _pg_major(config, source):
    target = str(config.target_major or source)
    check_engine("postgres", target)
    if target not in PG_MAJORS or not source.isdigit() or not 12 <= int(source) <= int(target):
        _fail("unsupported_postgres_version", "PostgreSQL logical restore requires an explicit supported non-downgrade target (16 or 17)")
    return target


def _pg_metadata(config, major, db, env, archive):
    icu_column = "datlocale" if int(major) >= 17 else "daticulocale"
    sql = """SELECT json_build_object(
        'name', d.datname, 'owner', (SELECT rolname FROM pg_roles WHERE oid=d.datdba), 'encoding', pg_encoding_to_char(d.encoding),
        'lc_collate', d.datcollate, 'lc_ctype', d.datctype,
        'locale_provider', d.datlocprovider, 'icu_locale', d.%s,
        'collation_version', d.datcollversion,
        'extensions', (SELECT COALESCE(json_agg(json_build_object('name',extname,'version',extversion,'schema',(SELECT nspname FROM pg_namespace WHERE oid=extnamespace),'owner',(SELECT rolname FROM pg_roles WHERE oid=extowner)) ORDER BY extname), '[]'::json) FROM pg_extension),
        'external_tablespaces', (SELECT count(*) FROM pg_tablespace WHERE spcname NOT IN ('pg_default','pg_global'))
        ) FROM pg_database d WHERE datname=current_database();""" % icu_column
    # Source catalog layout depends on source server, not the newer client.
    value = _json_output(_pg_query(config, major, sql, env))
    if not isinstance(value, dict) or value.get("name") != db:
        _fail("invalid_database_metadata", "Database metadata does not match the requested database")
    if value.pop("external_tablespaces", 1):
        _fail("unsupported_tablespace", "External PostgreSQL tablespaces require an explicit separate restore plan")
    value["archive"] = archive
    return value


def _export_postgres(config, stage):
    names = _names(config)
    env = _pg_conn(config, names[0])
    probe = _pg_query(config, pg_probe_major(config), "SELECT current_setting('server_version_num');", env)
    try:
        source = str(int(probe.stdout.strip()) // 10000)
    except (ValueError, TypeError):
        _fail("invalid_database_metadata", "The PostgreSQL server major version is invalid")
    target = _pg_major(config, source)
    databases = []
    for i, db in enumerate(names):
        conn = _pg_conn(config, db)
        name = "db-%04d.dump" % i
        # PG12-14 lack ICU database-level metadata; datlocprovider arrived in 15.
        if int(source) < 15:
            sql = "SELECT json_build_object('name',datname,'owner',(SELECT rolname FROM pg_roles WHERE oid=datdba),'encoding',pg_encoding_to_char(encoding),'lc_collate',datcollate,'lc_ctype',datctype,'locale_provider','c','icu_locale',NULL,'collation_version',NULL,'extensions',(SELECT COALESCE(json_agg(json_build_object('name',extname,'version',extversion,'schema',(SELECT nspname FROM pg_namespace WHERE oid=extnamespace),'owner',(SELECT rolname FROM pg_roles WHERE oid=extowner)) ORDER BY extname),'[]'::json) FROM pg_extension),'external_tablespaces',(SELECT count(*) FROM pg_tablespace WHERE spcname NOT IN ('pg_default','pg_global'))) FROM pg_database WHERE datname=current_database();"
            item = _json_output(_pg_query(config, target, sql, conn))
            if not isinstance(item, dict) or item.get("name") != db or item.pop("external_tablespaces", 1):
                _fail("unsupported_tablespace", "PostgreSQL metadata or tablespace requirements are unsupported")
            item["archive"] = name
        else:
            # Use source catalog layout with target binaries.
            icu = "datlocale" if int(source) >= 17 else "daticulocale"
            sql = "SELECT json_build_object('name',datname,'owner',(SELECT rolname FROM pg_roles WHERE oid=datdba),'encoding',pg_encoding_to_char(encoding),'lc_collate',datcollate,'lc_ctype',datctype,'locale_provider',datlocprovider,'icu_locale',%s,'collation_version',datcollversion,'extensions',(SELECT COALESCE(json_agg(json_build_object('name',extname,'version',extversion,'schema',(SELECT nspname FROM pg_namespace WHERE oid=extnamespace),'owner',(SELECT rolname FROM pg_roles WHERE oid=extowner)) ORDER BY extname),'[]'::json) FROM pg_extension),'external_tablespaces',(SELECT count(*) FROM pg_tablespace WHERE spcname NOT IN ('pg_default','pg_global'))) FROM pg_database WHERE datname=current_database();" % icu
            item = _json_output(_pg_query(config, target, sql, conn))
            if not isinstance(item, dict) or item.get("name") != db or item.pop("external_tablespaces", 1):
                _fail("unsupported_tablespace", "PostgreSQL metadata or tablespace requirements are unsupported")
            item["archive"] = name
        _call(config, [_pg_bin(config, target, "pg_dump"), "--format=custom", "--create", "--no-password", "--dbname=" + conn["PGDATABASE"]], env=conn, stdout_path=stage / name)
        _archive(stage, name)
        # A TOC listing is insufficient: decode every custom-archive block.
        _call(config, [_pg_bin(config, target, "pg_restore"), "--exit-on-error", "--file=/dev/null", str(stage / name)])
        databases.append(item)
    _call(config, [_pg_bin(config, target, "pg_dumpall"), "--globals-only", "--no-role-passwords", "--no-tablespaces", "--no-password",
                   "--dbname=" + env["PGDATABASE"]], env=env, stdout_path=stage / "globals.sql")
    _archive(stage, "globals.sql")
    roles = _json_output(_pg_query(config, target, "SELECT COALESCE(json_agg(rolname ORDER BY rolname),'[]'::json) FROM pg_roles WHERE rolcanlogin AND rolname !~ '^pg_';", env))
    if not isinstance(roles, list) or any(not isinstance(r, str) for r in roles):
        _fail("invalid_database_metadata", "PostgreSQL role metadata is invalid")
    return {"engine": "postgres", "source_major": source, "target_major": target,
            "databases": databases, "globals": "globals.sql", "login_roles": roles,
            "globals_contains_auth_hash": False, "consistency": "per-database-native-snapshot;globals-separate;quiesce-ddl"}


def _normal_locale(value):
    return value.lower().replace("-", "").replace(".utf8", ".utf8")


def _check_pg_requirements(config, major, databases):
    available = _call(config, [_binary(config, "LOCALE_BIN", "/usr/bin/locale"), "-a"]).stdout.decode("utf-8", "strict").splitlines()
    locales = {_normal_locale(x) for x in available}
    share = Path(_env(config).get("PG_SHARE_DIR_" + major, "/usr/share/postgresql/" + major)) / "extension"
    for item in databases:
        if not isinstance(item, dict) or item.get("locale_provider") not in {"c", "i"}:
            _fail("unsupported_locale", "This PostgreSQL locale provider requires an explicit restore plan")
        if not isinstance(item.get("encoding"), str) or not re.fullmatch(r"[A-Za-z0-9_]+", item["encoding"]):
            _fail("unsupported_encoding", "The PostgreSQL encoding metadata is unsupported")
        if item["locale_provider"] == "c":
            if any(not isinstance(item.get(k), str) or _normal_locale(item[k]) not in locales for k in ("lc_collate", "lc_ctype")):
                _fail("locale_missing", "A required source database locale is unavailable")
        elif not isinstance(item.get("icu_locale"), str) or not item["icu_locale"]:
            _fail("unsupported_locale", "The ICU locale metadata is incomplete")
        extensions = item.get("extensions")
        if not isinstance(extensions, list):
            _fail("invalid_database_metadata", "PostgreSQL extension metadata is invalid")
        for ext in extensions:
            if not isinstance(ext, dict) or not re.fullmatch(r"[A-Za-z0-9_-]+", str(ext.get("name", ""))) or not re.fullmatch(r"[A-Za-z0-9_.+-]{1,80}", str(ext.get("version", ""))):
                _fail("unsupported_extension", "An extension identifier is unsupported")
            try:
                control = (share / (ext["name"] + ".control")).read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                _fail("unsupported_extension", "A required PostgreSQL extension is unavailable")
            match = re.search(r"(?m)^\s*default_version\s*=\s*'([^']+)'", control)
            if not match or (match.group(1) != ext["version"] and not (share / (ext["name"] + "--" + ext["version"] + ".sql")).is_file()):
                _fail("unsupported_extension_version", "The snapshot extension version has no available installation SQL; an explicit extension migration is required")
            if ext.get("schema", "public") not in {"public", "pg_catalog"}:
                _fail("unsupported_extension_schema", "An extension in a custom schema requires an explicit restore migration")


def _scram(password):
    # Produce the verifier in memory: never persist supplied plaintext passwords.
    salt = secrets.token_bytes(16)
    # PostgreSQL SASLprep-normalizes valid Unicode passwords and falls back to
    # their original bytes if preparation fails (RFC 4013 / SCRAM semantics).
    mapped = "".join(" " if stringprep.in_table_c12(c) else c for c in password if not stringprep.in_table_b1(c))
    prepared = unicodedata.ucd_3_2_0.normalize("NFKC", mapped)
    prohibited = (stringprep.in_table_a1, stringprep.in_table_c12, stringprep.in_table_c21,
                  stringprep.in_table_c22, stringprep.in_table_c3, stringprep.in_table_c4,
                  stringprep.in_table_c5, stringprep.in_table_c6, stringprep.in_table_c7,
                  stringprep.in_table_c8, stringprep.in_table_c9)
    valid = not any(test(c) for c in prepared for test in prohibited)
    if any(stringprep.in_table_d1(c) for c in prepared):
        valid = valid and not any(stringprep.in_table_d2(c) for c in prepared)
        valid = valid and bool(prepared) and stringprep.in_table_d1(prepared[0]) and stringprep.in_table_d1(prepared[-1])
    raw = (prepared if valid else password).encode("utf-8")
    salted = hashlib.pbkdf2_hmac("sha256", raw, salt, 4096)
    client = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored = hashlib.sha256(client).digest()
    server = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda x: base64.b64encode(x).decode("ascii")
    return "SCRAM-SHA-256$4096:" + b64(salt) + "$" + b64(stored) + ":" + b64(server)


def _role_passwords(config):
    values = {}
    filename = _env(config).get("RESTORE_ROLE_PASSWORDS_FILE")
    if filename:
        try:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                s = os.fstat(stream.fileno())
                if not stat.S_ISREG(s.st_mode) or stat.S_IMODE(s.st_mode) & 0o077 or s.st_size > 1024 * 1024:
                    _fail("unsafe_role_password_file", "The role password mapping must be a private regular JSON file")
                values = json.loads(stream.read().decode("utf-8"))
        except (OSError, UnicodeError, ValueError):
            _fail("invalid_role_password_file", "The role password mapping could not be read")
        if not isinstance(values, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not v or "\0" in v for k, v in values.items()):
            _fail("invalid_role_password_file", "The role password mapping must map role names to nonempty passwords")
    if config.db_password:
        values[config.db_user] = config.db_password
    return values


def _pg_final_config(target, identity):
    _write_private(target / "pg_hba.conf", "# Restored logical cluster: no trust rules survive publication.\nlocal all all scram-sha-256\nhost all all 0.0.0.0/0 scram-sha-256\nhost all all ::/0 scram-sha-256\n", *identity, replace=True)
    # The temporary server overrides listen_addresses; this is for final startup.
    with (target / "postgresql.conf").open("a", encoding="utf-8") as f:
        f.write("\nlisten_addresses = '*'\npassword_encryption = 'scram-sha-256'\n")
        f.flush()
        os.fsync(f.fileno())


@contextmanager
def _pg_server(config, major, target, runtime, identity, *, hba=None):
    options = ["-c", "listen_addresses=", "-c", "data_directory=" + str(target), "-c", "unix_socket_directories=" + str(runtime),
               "-c", "unix_socket_permissions=0700", "-c", "logging_collector=off",
               "-c", "log_statement=none", "-c", "log_min_error_statement=panic",
               "-c", "shared_buffers=32MB", "-c", "max_connections=20", "-c", "jit=off",
               "-c", "fsync=on", "-c", "full_page_writes=on",
               "-c", "archive_mode=off", "-c", "archive_command=", "-c", "restore_command=",
               "-c", "archive_cleanup_command=", "-c", "recovery_end_command=", "-c", "primary_conninfo=",
               "-c", "shared_preload_libraries=", "-c", "session_preload_libraries=", "-c", "local_preload_libraries="]
    if hba is not None:
        options += ["-c", "hba_file=" + str(hba)]
    argv = [_pg_bin(config, major, "postgres"), "-D", str(target)] + options
    try:
        proc = subprocess.Popen(_as_database(config, argv, identity, parent_death=True), env=dict(_BASE_ENV), cwd=runtime,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        _fail("tool_unavailable", "The temporary PostgreSQL server could not be started")
    clean = False
    try:
        until = time.monotonic() + min(config.run_timeout, 60)
        ready = runtime / ".s.PGSQL.5432"
        while True:
            if ready.exists():
                probe = run([_pg_bin(config, major, "pg_isready"), "-q", "-h", str(runtime), "-p", "5432", "-t", "1"],
                            env=dict(_BASE_ENV), timeout=3, check=False)
                if probe.returncode == 0:
                    break
            if proc.poll() is not None:
                _fail("database_start_failed", "The temporary PostgreSQL server exited before readiness")
            if time.monotonic() >= until:
                _fail("database_start_timeout", "The temporary PostgreSQL server did not become ready")
            time.sleep(0.05)
        yield
        # Fast shutdown rolls back any remaining transactions and checkpoints.
        _signal_database(config, proc, signal.SIGINT, identity)
        try:
            code = proc.wait(timeout=min(config.run_timeout, 60))
        except subprocess.TimeoutExpired:
            _fail("database_shutdown_timeout", "The temporary PostgreSQL server did not shut down cleanly")
        if code != 0:
            _fail("database_shutdown_failed", "The temporary PostgreSQL server did not exit cleanly")
        clean = True
    finally:
        if not clean or proc.poll() is None:
            _kill_group(config, proc, identity)


def _manifest_engine(config, manifest):
    info = manifest.get("engine") if isinstance(manifest, dict) else None
    if not isinstance(info, dict) or info.get("engine") != config.engine:
        _fail("database_engine_mismatch", "Snapshot database engine does not match configuration")
    entries = info.get("databases")
    if not isinstance(entries, list) or not entries or any(not isinstance(d, dict) for d in entries):
        _fail("invalid_database_metadata", "Snapshot database list is invalid")
    names = [d.get("name") for d in entries]
    if names != _names(config):
        _fail("database_set_mismatch", "Snapshot databases do not match the configured database set")
    return info


def _create_pg_database(config, major, archive, db, conn, runtime):
    # Restore only native DATABASE metadata first so exact extension versions can
    # be installed before the main archive's version-less CREATE EXTENSION.
    result = _call(config, [_pg_bin(config, major, "pg_restore"), "--list", "--create", str(archive)])
    try:
        toc = result.stdout.decode("utf-8")
    except UnicodeError:
        _fail("invalid_database_archive", "The native archive table of contents is not UTF-8")
    selected = []
    for line in toc.splitlines():
        if re.match(r"^\d+; \d+ \d+ (?:DATABASE(?: PROPERTIES)? - |(?:COMMENT|ACL) - DATABASE )", line):
            selected.append(line)
    has_database = any(re.match(r"^\d+; \d+ \d+ DATABASE - ", x) for x in selected)
    if not has_database and not db.get("owner"):
        _fail("database_create_metadata_missing", "An archive without database creation metadata requires an explicit database owner")
    path = runtime / ("create-" + secrets.token_hex(8) + ".list")
    _write_private(path, "\n".join(selected) + "\n")
    try:
        if has_database:
            _call(config, [_pg_bin(config, major, "pg_restore"), "--exit-on-error", "--create", "--no-password",
                           "--use-list=" + str(path), "--dbname=" + conn["PGDATABASE"], str(archive)], env=conn)
        else:
            sql = ("CREATE DATABASE " + _pg_identifier(db["name"]) + " WITH TEMPLATE template0 OWNER " +
                   _pg_identifier(db["owner"]) + " ENCODING " + _pg_literal(db["encoding"]) +
                   " LC_COLLATE " + _pg_literal(db["lc_collate"]) + " LC_CTYPE " + _pg_literal(db["lc_ctype"]))
            if db["locale_provider"] == "i":
                sql += " LOCALE_PROVIDER icu ICU_LOCALE " + _pg_literal(db["icu_locale"])
            else:
                sql += " LOCALE_PROVIDER libc"
            _pg_query(config, major, sql + ";", conn)
        dbconn = dict(conn)
        escaped = db["name"].replace("\\", "\\\\").replace("'", "\\'")
        dbconn["PGDATABASE"] = "dbname='" + escaped + "'"
        sql = ""
        for ext in db["extensions"]:
            if ext["name"] == "plpgsql" and ext.get("owner"):
                sql += "DROP EXTENSION plpgsql;\n"
            elif ext["name"] == "plpgsql":
                continue  # Legacy metadata may not declare the source owner.
            if ext.get("owner"):
                sql += "SET ROLE " + _pg_identifier(ext["owner"]) + ";\n"
            sql += ("CREATE EXTENSION IF NOT EXISTS " + _pg_identifier(ext["name"]) + " WITH SCHEMA " +
                    _pg_identifier(ext.get("schema", "public")) + " VERSION " + _pg_literal(ext["version"]) + ";\nRESET ROLE;\n")
        if sql:
            _pg_query(config, major, sql, dbconn)
        _call(config, [_pg_bin(config, major, "pg_restore"), "--exit-on-error", "--no-password",
                       "--dbname=" + dbconn["PGDATABASE"], str(archive)], env=dbconn)
    finally:
        path.unlink()


def _restore_postgres(config, stage, target, info):
    source = str(info.get("source_major", ""))
    major = _pg_major(config, source)
    # A dump made by a newer client is not decodable by an older target.
    if major != info.get("target_major"):
        _fail("database_target_mismatch", "The PostgreSQL target major differs from the snapshot export target")
    entries = info["databases"]
    _check_pg_requirements(config, major, entries)
    archives = [_archive(stage, d.get("archive")) for d in entries]
    globals_file = _archive(stage, info.get("globals"))
    passwords = _role_passwords(config)
    login_roles = info.get("login_roles", [])
    if not isinstance(login_roles, list) or any(not isinstance(n, str) for n in login_roles):
        _fail("invalid_database_metadata", "PostgreSQL login role metadata is invalid")
    if set(passwords) - set(login_roles):
        _fail("unknown_restore_role", "A password mapping references a role absent from the snapshot")
    helper, final = _pg_restore_identities(config)
    initial = target.lstat()
    root_expected = (initial.st_dev, initial.st_ino)
    bootstrap = "k3s_restore_" + secrets.token_hex(12)
    with _database_access(config, target, "postgres", identity=helper) as identity, _runtime_dir(identity) as runtime:
        first = entries[0]
        init = [_pg_bin(config, major, "initdb"), "-D", str(target), "--username=" + bootstrap,
                "--encoding=" + first["encoding"], "--auth-local=trust", "--auth-host=scram-sha-256", "--no-instructions"]
        if first["locale_provider"] == "i":
            init += ["--locale-provider=icu", "--icu-locale=" + first["icu_locale"], "--locale=C.UTF-8"]
        else:
            init += ["--locale-provider=libc", "--lc-collate=" + first["lc_collate"], "--lc-ctype=" + first["lc_ctype"]]
        _call(config, _as_database(config, init, identity))
        conn = _pg_conn(config, "template1", socket=runtime, user=bootstrap)
        with _pg_server(config, major, target, runtime, identity):
            _call(config, [_pg_bin(config, major, "psql"), "-X", "-q", "--no-password", "--dbname=" + conn["PGDATABASE"], "-v", "ON_ERROR_STOP=1", "-f", "-"], env=conn, input_path=globals_file)
            for db, archive in zip(entries, archives):
                # postgres is pre-created by initdb; template1 is not a payload DB.
                if db["name"] == "postgres":
                    _pg_query(config, major, "DROP DATABASE postgres;", conn)
                _create_pg_database(config, major, archive, db, conn, runtime)
                restored = _pg_metadata(config, major, db["name"], _pg_conn(config, db["name"], socket=runtime, user=bootstrap), db["archive"])
                for k in ("encoding", "lc_collate", "lc_ctype", "locale_provider", "icu_locale"):
                    if restored.get(k) != db.get(k):
                        _fail("database_restore_mismatch", "Restored database locale or extensions differ from the snapshot")
                actual_ext = {x["name"]: x for x in restored.get("extensions", [])}
                for expected_ext in db["extensions"]:
                    actual = actual_ext.get(expected_ext["name"], {})
                    if any(actual.get(key) != value for key, value in expected_ext.items()):
                        _fail("database_restore_mismatch", "Restored extension metadata differs from the snapshot")
                if len(actual_ext) != len(db["extensions"]):
                    _fail("database_restore_mismatch", "Restored extension set differs from the snapshot")
                # Warn/fail instead of silently accepting different libc/ICU sort semantics.
                if db.get("collation_version") is not None and restored.get("collation_version") != db["collation_version"]:
                    _fail("collation_version_mismatch", "The database collation library version differs from the source")
            sql = "".join("ALTER ROLE " + _pg_identifier(role) + " PASSWORD " + _pg_literal(_scram(password)) + ";\n" for role, password in passwords.items())
            # initdb owns catalog/template objects: keep its owner role, disallow login.
            sql += "ALTER ROLE " + _pg_identifier(bootstrap) + " NOLOGIN;\nCHECKPOINT;"
            _pg_query(config, major, sql, conn)
        _pg_final_config(target, identity)
        _fsync_tree(target)
        if helper != final:
            control = _call(config, [_pg_bin(config, major, "pg_controldata"), str(target)]).stdout
            if not re.search(rb"(?m)^Database cluster state:\s*shut down\s*$", control):
                _fail("database_handoff_active", "Database ownership requires verified clean shutdown control data")
            _handoff_database_owner(target, helper, final, root_expected, clean_shutdown=True)
    if set(login_roles) - set(passwords):
        warnings.warn("restore_role_passwords_missing: some login roles have no password; configure RESTORE_ROLE_PASSWORDS_FILE before cutover", RuntimeWarning, stacklevel=2)


def _maria_literal(value):
    if not isinstance(value, str) or "\0" in value:
        _fail("invalid_database_metadata", "Invalid MariaDB text metadata")
    return "CONVERT(X'" + value.encode("utf-8").hex() + "' USING utf8mb4)"


def _maria_account(user, host):
    # Account grammar requires literal strings, not expressions. Set sql_mode=''
    # before executing these generated account statements.
    def quote(value):
        if not isinstance(value, str) or "\0" in value:
            _fail("invalid_database_metadata", "Invalid MariaDB account metadata")
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r") + "'"
    return quote(user) + "@" + quote(host)


def _maria_hash(password):
    return "*" + hashlib.sha1(hashlib.sha1(password.encode()).digest()).hexdigest().upper()


def _maria_account_passwords(config):
    """Explicit exact-account overrides only; connection credentials are not intent.

    PostgreSQL's role-only mapping cannot represent MariaDB user@host and is
    deliberately rejected rather than silently ignored or broadcast to hosts.
    """
    env = _env(config)
    if "RESTORE_ROLE_PASSWORDS_FILE" in env:
        _fail("ambiguous_mariadb_password_mapping", "MariaDB requires an explicit user and host account mapping; role-only mappings are not supported")
    key = "RESTORE_MARIADB_ACCOUNT_PASSWORDS_FILE"
    if key not in env:
        return {}
    filename = env[key]
    if (not isinstance(filename, str) or not filename or not Path(filename).is_absolute()
            or "\0" in filename):
        _fail("invalid_mariadb_password_file", "The MariaDB account mapping must name an absolute private JSON file")
    try:
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_uid != os.geteuid() or before.st_nlink != 1
                    or not 0 < before.st_size <= 1024 * 1024):
                _fail("unsafe_mariadb_password_file", "The MariaDB account mapping must be an owner-only mode0600 regular file")
            raw = stream.read(1024 * 1024 + 1)
            after = os.fstat(stream.fileno())
            identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
            if identity(before) != identity(after) or len(raw) > 1024 * 1024:
                _fail("unsafe_mariadb_password_file", "The MariaDB account mapping changed while being read")
        def pairs(items):
            value = {}
            for name, item in items:
                if name in value:
                    _fail("invalid_mariadb_password_file", "The MariaDB account mapping contains duplicate fields")
                value[name] = item
            return value
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        _fail("invalid_mariadb_password_file", "The MariaDB account mapping could not be read or decoded")
    if (not isinstance(document, dict) or set(document) != {"schema_version", "accounts"}
            or type(document["schema_version"]) is not int or document["schema_version"] != 1
            or not isinstance(document["accounts"], list) or not 0 < len(document["accounts"]) <= 10000):
        _fail("invalid_mariadb_password_file", "The MariaDB account mapping schema is invalid")
    overrides = {}
    for item in document["accounts"]:
        if not isinstance(item, dict) or set(item) != {"user", "host", "plugin", "password"}:
            _fail("invalid_mariadb_password_file", "Every MariaDB override must specify user, host, plugin and password")
        if any(not isinstance(item[key], str) or not item[key] or any(ord(c) < 32 for c in item[key])
               for key in ("user", "host")):
            _fail("invalid_mariadb_password_file", "MariaDB account identities must be explicit nonempty strings")
        if item["plugin"] != "mysql_native_password":
            _fail("unsupported_mariadb_auth_override", "Only explicitly selected mysql_native_password overrides are supported")
        if not isinstance(item["password"], str) or not item["password"] or "\0" in item["password"]:
            _fail("invalid_mariadb_password_file", "The MariaDB account mapping contains an invalid identity or password")
        try:
            identity_size = (len(item["user"].encode("utf-8")), len(item["host"].encode("utf-8")))
            item["password"].encode("utf-8")
        except UnicodeError:
            _fail("invalid_mariadb_password_file", "The MariaDB account mapping contains invalid Unicode text")
        if identity_size[0] > 128 or identity_size[1] > 255:
            _fail("invalid_mariadb_password_file", "The MariaDB account mapping contains an invalid identity or password")
        account = (item["user"], item["host"])
        if account in overrides:
            _fail("invalid_mariadb_password_file", "The MariaDB account mapping repeats an account")
        overrides[account] = item["password"]
    return overrides


def _maria_auth_override(config, options, overrides):
    """Verify exact imported accounts, then apply and verify only their overrides."""
    for (user, host), password in overrides.items():
        where = "BINARY User=" + _maria_literal(user) + " AND BINARY Host=" + _maria_literal(host) + " AND is_role='N'"
        found = _maria_query(config, options, "SELECT COUNT(*) FROM mysql.user WHERE " + where + ";").stdout.strip()
        if found != b"1":
            _fail("unknown_restore_account", "An explicit MariaDB override does not identify one imported login account")
    for (user, host), password in overrides.items():
        verifier = _maria_hash(password)
        _maria_query(config, options, "SET sql_mode=''; ALTER USER " + _maria_account(user, host)
                     + " IDENTIFIED VIA mysql_native_password USING '" + verifier + "';")
        where = "BINARY User=" + _maria_literal(user) + " AND BINARY Host=" + _maria_literal(host) + " AND is_role='N'"
        verified = _maria_query(config, options, "SELECT COUNT(*) FROM mysql.user WHERE " + where
                                + " AND plugin='mysql_native_password' AND BINARY authentication_string="
                                + _maria_literal(verifier) + ";").stdout.strip()
        if verified != b"1":
            _fail("mariadb_auth_override_mismatch", "An explicit MariaDB authentication override could not be verified")


def _option_value(value):
    if "\0" in value:
        _fail("invalid_database_config", "MariaDB connection configuration contains invalid text")
    return '"' + value.replace(chr(92), chr(92) * 2).replace('"', chr(92) + '"').replace("\n", chr(92) + "n").replace("\r", chr(92) + "r") + '"'


@contextmanager
def _maria_options(config, runtime, *, socket=None, user=None, password=None):
    path = runtime / ("client-" + secrets.token_hex(8) + ".cnf")
    content = "[client]\nuser=" + _option_value(config.db_user if user is None else user) + "\npassword=" + _option_value(config.db_password if password is None else password) + "\n"
    sock = socket or _env(config).get("MARIADB_SOCKET") or _env(config).get("MYSQL_SOCKET")
    if sock is None and str(config.db_host).startswith("/"):
        sock = config.db_host
    if sock:
        content += "protocol=socket\nsocket=" + _option_value(str(sock)) + "\n"
    else:
        content += "protocol=tcp\nhost=" + _option_value(str(config.db_host)) + "\nport=" + _option_value(str(config.db_port)) + "\n"
    # Only explicit TLS options are inherited; ~/.my.cnf is never read.
    for env_key, option in (("MARIADB_SSL_CA", "ssl-ca"), ("MARIADB_SSL_CERT", "ssl-cert"), ("MARIADB_SSL_KEY", "ssl-key")):
        if socket is None and _env(config).get(env_key):
            content += option + "=" + _option_value(_env(config)[env_key]) + "\n"
    if socket is None and _env(config).get("MARIADB_SSL_CA"):
        content += "ssl-verify-server-cert=1\n"
    _write_private(path, content)
    try:
        yield path
    finally:
        path.unlink()


def _maria_query(config, options, sql):
    return _call(config, [_maria_bin(config, "client"), "--defaults-file=" + str(options), "--batch", "--raw",
                          "--skip-column-names", "--binary-mode", "--connect-timeout=10"], input_data=(sql + "\n").encode())


def _maria_version(value):
    match = re.search(r"(\d+\.\d+)\.\d+[^\s]*-MariaDB", value, re.IGNORECASE)
    if not match:
        _fail("unsupported_mariadb_version", "The database must identify as supported MariaDB, not MySQL")
    return match.group(1)


def _maria_target(config, source):
    target = str(config.target_major or source)
    if source not in MARIA_SOURCES or target != "11.8" or tuple(map(int, source.split("."))) > (11, 8):
        _fail("unsupported_mariadb_version", "MariaDB logical restore requires a supported source and explicit 11.8 upgrade target")
    local = _call(config, [_maria_bin(config, "server"), "--no-defaults", "--version"]).stdout.decode("ascii", "replace")
    if _maria_version(local) != target:
        _fail("mariadb_binary_mismatch", "The installed MariaDB server does not match the target release series")
    return target


def _maria_login_query():
    return ("SELECT COALESCE(JSON_ARRAYAGG(JSON_OBJECT('user',User,'host',Host,"
            "'global_grant_option',IF(Grant_priv='Y',TRUE,FALSE))),JSON_ARRAY()) FROM mysql.user WHERE is_role='N';")


def _maria_login_metadata(value, *, native=False):
    if not isinstance(value, list) or len(value) > 100000:
        _fail("invalid_mariadb_login_metadata", "MariaDB login metadata must be an explicit bounded list")
    result, seen = [], set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"user", "host", "global_grant_option"}:
            _fail("mariadb_global_grant_metadata_required", "Every MariaDB login requires explicit global grant-option metadata")
        grant = item["global_grant_option"]
        if native and type(grant) is int and grant in (0, 1):
            grant = bool(grant)
        if type(grant) is not bool:
            _fail("invalid_mariadb_login_metadata", "MariaDB global grant option must be an explicit boolean")
        for key in ("user", "host"):
            if not isinstance(item[key], str) or any(ord(c) < 32 for c in item[key]) or len(item[key]) > 255:
                _fail("invalid_mariadb_login_metadata", "MariaDB login identity metadata is invalid")
            try:
                item[key].encode("utf-8")
            except UnicodeError:
                _fail("invalid_mariadb_login_metadata", "MariaDB login identity metadata is invalid")
        account = (item["user"], item["host"])
        if account in seen:
            _fail("invalid_mariadb_login_metadata", "MariaDB login metadata repeats an account")
        seen.add(account)
        result.append({"user": account[0], "host": account[1], "global_grant_option": grant})
    return sorted(result, key=lambda item: (item["user"], item["host"]))


def _maria_login_read(config, options):
    return _maria_login_metadata(_json_output(_maria_query(config, options, _maria_login_query())), native=True)


def _maria_scoped_grants(config, options, user, host):
    # Read only grant-option-bearing scope metadata. No mysql table import and
    # no user data: this guards REVOKE's effects on narrower privileges.
    where = "BINARY User=" + _maria_literal(user) + " AND BINARY Host=" + _maria_literal(host)
    tables = {"db": ("Host", "Db", "User", "Grant_priv"),
              "tables_priv": ("Host", "Db", "User", "Table_name", "Table_priv", "Column_priv"),
              "columns_priv": ("Host", "Db", "User", "Table_name", "Column_name", "Column_priv"),
              "procs_priv": ("Host", "Db", "User", "Routine_name", "Routine_type", "Proc_priv")}
    fields = []
    for table, columns in tables.items():
        record = ",".join("'" + name + "'," + name for name in columns)
        fields.append("'" + table + "',(SELECT COALESCE(JSON_ARRAYAGG(JSON_OBJECT(" + record
                      + ")),JSON_ARRAY()) FROM mysql." + table + " WHERE " + where + ")")
    data = _json_output(_maria_query(config, options, "SELECT JSON_OBJECT(" + ",".join(fields) + ");"))
    if not isinstance(data, dict) or set(data) != set(tables) or any(not isinstance(rows, list) for rows in data.values()):
        _fail("invalid_mariadb_scoped_grants", "MariaDB scoped grant metadata could not be verified")
    return {table: sorted(json.dumps(row, sort_keys=True, ensure_ascii=True) for row in rows)
            for table, rows in data.items()}


def _maria_global_grant_sync(config, options, accounts):
    current = {(item["user"], item["host"]): item["global_grant_option"] for item in _maria_login_read(config, options)}
    repairs = []
    for account in accounts:
        key = (account["user"], account["host"])
        if key not in current or (account["global_grant_option"] and not current[key]):
            _fail("mariadb_global_grant_restore_mismatch", "A required MariaDB global grant option is absent; no implicit privilege grant is allowed")
        if not account["global_grant_option"] and current[key]:
            repairs.append(key)
    for user, host in repairs:
        before = _maria_scoped_grants(config, options, user, host)
        _maria_query(config, options, "SET sql_mode=''; REVOKE GRANT OPTION ON *.* FROM "
                     + _maria_account(user, host) + "; FLUSH PRIVILEGES;")
        if _maria_scoped_grants(config, options, user, host) != before:
            _fail("mariadb_scoped_grant_restore_mismatch", "Removing an extra global grant option changed narrower MariaDB grants")
    final = {(item["user"], item["host"]): item["global_grant_option"] for item in _maria_login_read(config, options)}
    if any(final.get((item["user"], item["host"])) is not item["global_grant_option"] for item in accounts):
        _fail("mariadb_global_grant_restore_mismatch", "Restored MariaDB global grant options differ from snapshot metadata")


def _maria_proxy_query():
    return ("SELECT COALESCE(JSON_ARRAYAGG(JSON_OBJECT('user',User,'host',Host,"
            "'proxied_user',Proxied_user,'proxied_host',Proxied_host,"
            "'with_grant',IF(With_grant=1,TRUE,FALSE))),JSON_ARRAY()) FROM mysql.proxies_priv;")


def _maria_proxy_grants(value, accounts):
    """Validate exact semantic tuples, not native GRANT's normalized host text."""
    if not isinstance(value, list) or len(value) > 100000:
        _fail("invalid_mariadb_proxy_metadata", "MariaDB PROXY metadata must be an explicit bounded list")
    known = {(item["user"], item["host"]) for item in accounts}
    result, seen = [], set()
    fields = ("user", "host", "proxied_user", "proxied_host")
    for grant in value:
        if not isinstance(grant, dict) or set(grant) != {*fields, "with_grant"}:
            _fail("invalid_mariadb_proxy_metadata", "MariaDB PROXY metadata has invalid fields")
        for field in fields:
            text = grant[field]
            if (not isinstance(text, str) or any(ord(c) < 32 for c in text)
                    or len(text) > 255):
                _fail("invalid_mariadb_proxy_metadata", "MariaDB PROXY account metadata contains invalid text")
            try:
                text.encode("utf-8")
            except UnicodeError:
                _fail("invalid_mariadb_proxy_metadata", "MariaDB PROXY account metadata contains invalid text")
        if type(grant["with_grant"]) is not bool:
            _fail("invalid_mariadb_proxy_metadata", "MariaDB PROXY grant option must be an explicit boolean")
        identity = tuple(grant[field] for field in fields)
        if identity in seen:
            _fail("invalid_mariadb_proxy_metadata", "MariaDB PROXY metadata repeats an account pair")
        if identity[:2] not in known:
            _fail("unknown_mariadb_proxy_account", "A MariaDB PROXY grantee is absent from snapshot accounts")
        # Empty proxied_user is MariaDB's wildcard/anonymous PROXY target;
        # an empty host is meaningful and MUST NOT be normalized to percent.
        if grant["proxied_user"] and identity[2:] not in known:
            _fail("unknown_mariadb_proxy_account", "A MariaDB PROXY target is absent from snapshot accounts")
        seen.add(identity)
        result.append(dict(grant))
    return sorted(result, key=lambda grant: tuple(grant[field] for field in fields))


def _maria_proxy_read(config, options, accounts):
    raw = _json_output(_maria_query(config, options, _maria_proxy_query()))
    if not isinstance(raw, list):
        _fail("invalid_mariadb_proxy_metadata", "MariaDB PROXY query returned invalid metadata")
    # MariaDB JSON_OBJECT renders IF(...,TRUE,FALSE) as numeric 0/1.
    for grant in raw:
        if isinstance(grant, dict) and type(grant.get("with_grant")) is int and grant["with_grant"] in (0, 1):
            grant["with_grant"] = bool(grant["with_grant"])
    return _maria_proxy_grants(raw, accounts)


def _maria_verify_logins(config, options, accounts, bootstrap):
    identities = {(item["user"], item["host"]) for item in accounts} | {(bootstrap, "localhost")}
    predicates = ["(BINARY User=" + _maria_literal(user) + " AND BINARY Host=" + _maria_literal(host) + ")"
                  for user, host in sorted(identities)]
    size = str(len(identities))
    sql = "SELECT IF(COUNT(*)=" + size + " AND SUM(" + " OR ".join(predicates) + ")=" + size + ",1,0) FROM mysql.user WHERE is_role='N';"
    if _maria_query(config, options, sql).stdout.strip() != b"1":
        _fail("mariadb_account_restore_mismatch", "Restored MariaDB login accounts differ from snapshot metadata")


def _maria_proxy_sync(config, options, grants, accounts, bootstrap):
    """Only called on this adapter's fresh, socket-only, unpublished target.

    Modify only mysql.proxies_priv semantic columns. All other mysql tables
    remain owned by native users/grants import. The bootstrap account keeps its
    global ALL privileges; its import-only PROXY is no longer needed here.
    """
    expected = _maria_proxy_grants(grants, accounts)
    values = []
    for grant in expected:
        values.append("(" + ",".join(_maria_literal(grant[field]) for field in
                                     ("host", "user", "proxied_host", "proxied_user"))
                      + "," + ("1" if grant["with_grant"] else "0") + ")")
    sql = "DELETE FROM mysql.proxies_priv;"
    if values:
        sql += "INSERT INTO mysql.proxies_priv (Host,User,Proxied_host,Proxied_user,With_grant) VALUES " + ",".join(values) + ";"
    _maria_query(config, options, sql + " FLUSH PRIVILEGES;")
    if _maria_proxy_read(config, options, accounts) != expected:
        _fail("mariadb_proxy_restore_mismatch", "Restored MariaDB PROXY permissions differ from snapshot metadata")


def _maria_innodb(config, options, names):
    literal = ",".join(_maria_literal(n) for n in names)
    result = _maria_query(config, options, "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA IN (" + literal + ") AND TABLE_TYPE='BASE TABLE' AND (ENGINE IS NULL OR ENGINE <> 'InnoDB');")
    if result.stdout.strip() != b"0":
        _fail("nontransactional_mariadb_tables", "Single-transaction backup requires exclusively InnoDB base tables")


def _export_mariadb(config, stage):
    names = _names(config)
    # No plaintext client option file is ever placed under the snapshot stage.
    current = (os.geteuid(), os.getegid())
    with _runtime_dir(current) as runtime, _maria_options(config, runtime) as options:
        source = _maria_version(_maria_query(config, options, "SELECT VERSION();").stdout.decode("ascii", "replace"))
        target = _maria_target(config, source)
        _maria_innodb(config, options, names)
        entries = []
        for name in names:
            value = _json_output(_maria_query(config, options, "SELECT JSON_OBJECT('name',SCHEMA_NAME,'character_set',DEFAULT_CHARACTER_SET_NAME,'collation',DEFAULT_COLLATION_NAME) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=" + _maria_literal(name) + ";"))
            if not isinstance(value, dict) or value.get("name") != name:
                _fail("invalid_database_metadata", "A declared MariaDB database is unavailable")
            value["archive"] = "databases.sql"
            entries.append(value)
        users = _maria_login_read(config, options)
        proxy_grants = _maria_proxy_read(config, options, users)
        # --replace globals preserves grant/account attributes, including auth hashes.
        _call(config, [_maria_bin(config, "dump"), "--defaults-file=" + str(options), "--system=users", "--replace",
                       "--skip-comments"], stdout_path=stage / "globals.sql")
        _call(config, [_maria_bin(config, "dump"), "--defaults-file=" + str(options), "--single-transaction", "--quick",
                       "--skip-lock-tables", "--routines", "--events", "--triggers", "--hex-blob", "--dump-history",
                       "--add-drop-database", "--default-character-set=utf8mb4", "--databases"] + names, stdout_path=stage / "databases.sql")
        _archive(stage, "globals.sql")
        _archive(stage, "databases.sql")
        _maria_innodb(config, options, names)
        if _maria_login_read(config, options) != users:
            _fail("mariadb_accounts_changed_during_export", "MariaDB accounts or global grant options changed during export; quiesce account changes")
        if _maria_proxy_read(config, options, users) != proxy_grants:
            _fail("mariadb_proxy_changed_during_export", "MariaDB PROXY permissions changed during export; quiesce account changes")
    return {"engine": "mariadb", "source_major": source, "target_major": target, "databases": entries,
            "globals": "globals.sql", "login_accounts": users, "proxy_grants": proxy_grants, "globals_contains_auth_hash": True,
            "consistency": "single-transaction-InnoDB-all-declared-databases;globals-separate;quiesce-ddl"}


def _signal_database(config, proc, sig, identity, *, group=False):
    # root without CAP_KILL cannot signal a dedicated database UID. Drop to that
    # same UID for signaling instead of requiring additional capabilities.
    argv = _as_database(config, [_binary(config, "KILL_BIN", "/bin/kill"),
                                "-" + str(int(sig)), "--", str(-proc.pid if group else proc.pid)], identity)
    result = run(argv, env=dict(_BASE_ENV), timeout=10, check=False)
    if result.returncode and proc.poll() is None:
        _fail("database_signal_failed", "The temporary database process could not be signalled")


def _kill_group(config, proc, identity):
    for sig, deadline in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        _signal_database(config, proc, sig, identity, group=True)
        try:
            proc.wait(timeout=deadline)
        except subprocess.TimeoutExpired:
            continue
        # Reap grandchildren even when the group leader already exited.
        _signal_database(config, proc, signal.SIGKILL, identity, group=True)
        return
    _fail("database_shutdown_timeout", "The temporary database process could not be reaped")

@contextmanager
def _maria_server(config, target, runtime, identity):
    socket = runtime / "server.sock"
    argv = [_maria_bin(config, "server"), "--no-defaults", "--datadir=" + str(target), "--socket=" + str(socket),
            "--pid-file=" + str(target / "server.pid"), "--log-error=" + str(runtime / "server.log"),
            "--skip-networking", "--skip-log-bin", "--general-log=0", "--slow-query-log=0",
            "--tmpdir=" + str(runtime), "--event-scheduler=OFF", "--innodb-buffer-pool-size=64M", "--innodb-log-file-size=8M", "--key-buffer-size=8M",
            "--max-connections=20", "--innodb-flush-log-at-trx-commit=1", "--innodb-fast-shutdown=0"]
    try:
        proc = subprocess.Popen(_as_database(config, argv, identity, parent_death=True), env=dict(_BASE_ENV), cwd=runtime,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        _fail("tool_unavailable", "The temporary MariaDB server could not be started")
    clean = False
    try:
        until = time.monotonic() + min(config.run_timeout, 60)
        while True:
            if socket.exists():
                probe = run([_maria_bin(config, "admin"), "--no-defaults", "--protocol=socket",
                             "--socket=" + str(socket), "--user=root", "--skip-password", "--connect-timeout=1", "ping"],
                            env=dict(_BASE_ENV), timeout=3, check=False)
                if probe.returncode == 0:
                    break
            if proc.poll() is not None:
                _fail("database_start_failed", "The temporary MariaDB server exited before readiness")
            if time.monotonic() >= until:
                _fail("database_start_timeout", "The temporary MariaDB server did not become ready")
            time.sleep(0.05)
        yield socket
        # SIGTERM is MariaDB's documented clean shutdown; bootstrap is now removed.
        _signal_database(config, proc, signal.SIGTERM, identity)
        try:
            code = proc.wait(timeout=min(config.run_timeout, 60))
        except subprocess.TimeoutExpired:
            _fail("database_shutdown_timeout", "The temporary MariaDB server did not shut down cleanly")
        if code != 0:
            _fail("database_shutdown_failed", "The temporary MariaDB server did not exit cleanly")
        clean = True
    finally:
        if not clean or proc.poll() is None:
            _kill_group(config, proc, identity)


def _restore_mariadb(config, stage, target, info):
    target_series = _maria_target(config, str(info.get("source_major", "")))
    if info.get("target_major") != target_series:
        _fail("database_target_mismatch", "The MariaDB target release series differs from the snapshot target")
    entries = info["databases"]
    archives = {str(_archive(stage, item.get("archive"))) for item in entries}
    if len(archives) != 1:
        _fail("invalid_database_metadata", "MariaDB requires a single all-database transactional archive")
    globals_file = _archive(stage, info.get("globals"))
    accounts = _maria_login_metadata(info.get("login_accounts"))
    source_accounts = {(a["user"], a["host"]) for a in accounts}
    if len(source_accounts) != len(accounts):
        _fail("invalid_database_metadata", "MariaDB login account metadata contains duplicate identities")
    if "proxy_grants" not in info:
        _fail("mariadb_proxy_metadata_required", "This snapshot lacks exact MariaDB PROXY metadata; a new verified export is required")
    proxy_grants = _maria_proxy_grants(info["proxy_grants"], accounts)
    overrides = _maria_account_passwords(config)
    if set(overrides) - source_accounts:
        _fail("unknown_restore_account", "An explicit password mapping references an absent MariaDB user and host")
    bootstrap = "k3s_restore_" + secrets.token_hex(8)
    if any(user == bootstrap for user, _ in source_accounts):
        _fail("restore_bootstrap_collision", "The temporary restore identity conflicts with a source account")
    bootstrap_password = secrets.token_hex(32)
    with _database_access(config, target, "mariadb") as identity, _runtime_dir(identity) as runtime:
        _call(config, _as_database(config, [_maria_bin(config, "install"), "--no-defaults", "--datadir=" + str(target),
                                            "--auth-root-authentication-method=normal", "--skip-test-db", "--innodb-log-file-size=8M"], identity), cwd=runtime)
        with _maria_server(config, target, runtime, identity) as socket:
            with _maria_options(config, runtime, socket=socket, user="root", password="") as root_opts:
                _maria_query(config, root_opts, "SET sql_mode=''; CREATE USER " + _maria_account(bootstrap, "localhost") +
                             " IDENTIFIED VIA mysql_native_password USING '" + _maria_hash(bootstrap_password) +
                             "'; GRANT ALL PRIVILEGES ON *.* TO " + _maria_account(bootstrap, "localhost") + " WITH GRANT OPTION;" +
                             " GRANT PROXY ON ''@'%' TO " + _maria_account(bootstrap, "localhost") + " WITH GRANT OPTION;")
            with _maria_options(config, runtime, socket=socket, user=bootstrap, password=bootstrap_password) as options:
                base = [_maria_bin(config, "client"), "--defaults-file=" + str(options), "--batch", "--binary-mode", "--show-warnings=0"]
                seed = _json_output(_maria_query(config, options, "SELECT COALESCE(JSON_ARRAYAGG(JSON_OBJECT('user',User,'host',Host)),JSON_ARRAY()) FROM mysql.user WHERE is_role='N';"))
                if not isinstance(seed, list):
                    _fail("invalid_database_metadata", "Fresh MariaDB account metadata is invalid")
                source_accounts = {(a["user"], a["host"]) for a in accounts}
                remove = "SET sql_mode='';\n"
                for account in seed:
                    if not isinstance(account, dict) or not isinstance(account.get("user"), str) or not isinstance(account.get("host"), str):
                        _fail("invalid_database_metadata", "Fresh MariaDB account metadata is invalid")
                    key = (account["user"], account["host"])
                    if key not in source_accounts and key != (bootstrap, "localhost"):
                        # mariadb.sys defines mysql.user and related system views.
                        # Retaining an extra account or deleting that definer is
                        # not an exact/safe restore: require a complete source.
                        if account["user"] == "mariadb.sys":
                            _fail("mariadb_system_account_metadata_required", "Source metadata lacks an installed MariaDB system view account")
                        remove += "DROP USER IF EXISTS " + _maria_account(*key) + ";\n"
                # Import native globals before cleaning fresh-install accounts.
                # In particular, mariadb.sys owns privilege-system views and must
                # not be removed: deleting it breaks native account operations.
                _call(config, base, input_path=globals_file)
                _call(config, base, input_path=Path(next(iter(archives))))
                for item in entries:
                    actual = _json_output(_maria_query(config, options, "SELECT JSON_OBJECT('name',SCHEMA_NAME,'character_set',DEFAULT_CHARACTER_SET_NAME,'collation',DEFAULT_COLLATION_NAME) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=" + _maria_literal(item["name"]) + ";"))
                    if any(actual.get(k) != item.get(k) for k in ("name", "character_set", "collation")):
                        _fail("database_restore_mismatch", "Restored MariaDB charset or collation differs from the snapshot")
                # Only this newly initialized unpublished target is changed.
                # Remove all source-absent seed logins (including the numeric
                # helper's NSS name), not just root/anonymous defaults.
                _maria_query(config, options, remove)
                # Native globals own authentication. Exact PROXY metadata, not
                # GRANT text normalization, owns the final proxy permission set.
                _maria_auth_override(config, options, overrides)
                _maria_verify_logins(config, options, accounts, bootstrap)
                _maria_proxy_sync(config, options, proxy_grants, accounts, bootstrap)
                _maria_global_grant_sync(config, options, accounts)
                if _maria_proxy_read(config, options, accounts) != proxy_grants:
                    _fail("mariadb_proxy_restore_mismatch", "Global grant-option reconciliation changed MariaDB PROXY permissions")
                _maria_query(config, options, "SET sql_mode=''; DROP USER " + _maria_account(bootstrap, "localhost") + "; FLUSH TABLES;")
        _fsync_tree(target)


def export_database(config, stage: Path) -> dict:
    """Export declared databases and globals into an empty private stage."""
    stage = _directory(stage, empty=True)
    os.chmod(stage, 0o700)
    if config.engine == "postgres":
        info = _export_postgres(config, stage)
    elif config.engine == "mariadb":
        info = _export_mariadb(config, stage)
    else:
        _fail("unsupported_engine", "Native export requires a configured database engine")
    _fsync_tree(stage)
    return info


def restore_database(config, stage: Path, target: Path, manifest: dict) -> None:
    """Restore already-verified logical payloads, never publish or delete a target."""
    stage = _directory(stage)
    target = _directory(target, empty=True)
    if stage == target or stage in target.parents or target in stage.parents:
        _fail("database_path_overlap", "Database input and output directories must not overlap")
    info = _manifest_engine(config, manifest)
    if config.engine == "postgres":
        _restore_postgres(config, stage, target, info)
    elif config.engine == "mariadb":
        _restore_mariadb(config, stage, target, info)
    else:
        _fail("unsupported_engine", "Native restore requires a configured database engine")


def export_physical(config, stage: Path) -> dict:
    """Native PG base backup only: never infer consistency from a live file copy."""
    stage = _directory(stage, empty=True)
    if config.engine != "postgres":
        _fail("physical_not_implemented", "MariaDB native physical backup and prepare are not implemented")
    names = _names(config)
    conn = _pg_conn(config, names[0])
    try:
        source = str(int(_pg_query(config, pg_probe_major(config), "SELECT current_setting('server_version_num');", conn).stdout.strip()) // 10000)
    except (ValueError, TypeError):
        _fail("invalid_database_metadata", "The PostgreSQL server version is invalid")
    if source not in PG_MAJORS or str(config.target_major or source) != source:
        _fail("physical_major_mismatch", "PostgreSQL physical backups cannot change server major version")
    tablespaces = _pg_query(config, source, "SELECT count(*) FROM pg_tablespace WHERE spcname NOT IN ('pg_default','pg_global');", conn)
    if tablespaces.stdout.strip() != b"0":
        _fail("unsupported_tablespace", "External physical tablespaces require a separate backup plan")
    payload = stage / "payload"
    _call(config, [_pg_bin(config, source, "pg_basebackup"), "--pgdata=" + str(payload),
                   "--format=plain", "--wal-method=stream", "--checkpoint=fast", "--no-password", "--dbname=" + conn["PGDATABASE"]], env=conn)
    _call(config, [_pg_bin(config, source, "pg_verifybackup"), str(payload)])
    databases = []
    for name in names:
        databases.append(_pg_metadata(config, source, name, _pg_conn(config, name), ""))
    _fsync_tree(stage)
    return {"engine": "postgres", "kind": "pg-physical", "source_major": source, "target_major": source,
            "physical_native": True, "payload": "payload", "databases": databases,
            "consistency": "native-pg-basebackup-with-streamed-WAL;entire-cluster"}


def validate_physical(config, target: Path, manifest: dict | None) -> None:
    """Conservative compatibility check; legacy live copies are never certified.

    Validation does not turn a legacy file copy into a consistent native backup.
    Crash/archive recovery needs an isolated restore rehearsal, not a false pass.
    """
    target = _directory(target)
    if config.engine != "postgres":
        _fail("physical_not_implemented", "MariaDB physical prepare and validation are not implemented")
    if manifest is None and not _enabled(config, "ALLOW_LEGACY_PHYSICAL"):
        _fail("legacy_physical_risk_required", "Legacy physical restore requires explicit ALLOW_LEGACY_PHYSICAL risk acknowledgement")
    info = manifest.get("engine", {}) if manifest else {}
    try:
        version = target / "PG_VERSION"
        if version.is_symlink():
            _fail("unsafe_database_tree", "PostgreSQL version metadata must not be a symlink")
        source = version.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        _fail("invalid_physical_backup", "PostgreSQL physical version metadata is unavailable")
    if source not in PG_MAJORS or str(config.target_major or source) != source or (info and info.get("source_major") != source):
        _fail("physical_major_mismatch", "PostgreSQL physical restore requires an exact supported major match")
    for root, dirs, files in os.walk(target, followlinks=False):
        for name in dirs + files:
            if (Path(root) / name).is_symlink():
                _fail("unsafe_database_tree", "Physical restore cannot follow links or external tablespaces")
    state = _call(config, [_pg_bin(config, source, "pg_controldata"), str(target)]).stdout.decode("ascii", "replace")
    match = re.search(r"(?m)^Database cluster state:\s*(.+)$", state)
    if not match:
        _fail("invalid_physical_backup", "PostgreSQL control data did not report a cluster state")
    native = info.get("physical_native") is True and info.get("kind") == "pg-physical"
    if match.group(1).strip() != "shut down" and not native:
        _fail("physical_recovery_required", "Legacy physical backup requires an isolated recovery plan before publication")
    if (target / "postmaster.pid").exists() or (target / "standby.signal").exists():
        _fail("physical_recovery_required", "Physical backup has live-process or standby markers")
    if native:
        _manifest_engine(config, manifest)
        _check_pg_requirements(config, source, info["databases"])
        # Before any recovery changes control/WAL files, verify every native
        # base-backup manifest checksum and required streamed WAL range again.
        _call(config, [_pg_bin(config, source, "pg_verifybackup"), str(target)])
        with _database_access(config, target, "postgres") as identity, _runtime_dir(identity) as runtime:
            for root, dirs, files in os.walk(target, followlinks=False):
                for name in dirs + files:
                    path = Path(root) / name
                    if os.geteuid() == 0:
                        os.chown(path, *identity, follow_symlinks=False)
            hba = runtime / "physical-hba.conf"
            _write_private(hba, "local all all trust\n", *identity)
            with _pg_server(config, source, target, runtime, identity, hba=hba):
                conn = _pg_conn(config, config.databases[0], socket=runtime, user=config.db_user)
                conn["PGPASSWORD"] = config.db_password
                status = _pg_query(config, source, "SELECT pg_is_in_recovery();", conn)
                if status.stdout.strip() != b"f":
                    _fail("physical_recovery_incomplete", "The native physical backup did not finish WAL recovery")
                passwords = _role_passwords(config)
                if passwords:
                    sql = "".join("ALTER ROLE " + _pg_identifier(role) + " PASSWORD " + _pg_literal(_scram(password)) + ";\n" for role, password in passwords.items())
                    _pg_query(config, source, sql, conn)
            _pg_final_config(target, identity)
            _fsync_tree(target)
        after = _call(config, [_pg_bin(config, source, "pg_controldata"), str(target)]).stdout
        if not re.search(rb"(?m)^Database cluster state:\s*shut down\s*$", after):
            _fail("physical_recovery_incomplete", "The native physical backup did not reach a clean shutdown")
    if manifest is None:
        warnings.warn("legacy_physical_consistency_unknown: clean control data does not prove a consistent live file copy", RuntimeWarning, stacklevel=2)


def _private_json(config, key):
    filename = _env(config).get(key)
    if not filename:
        _fail("legacy_metadata_required", "Legacy native archives require explicit database metadata")
    try:
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            s = os.fstat(stream.fileno())
            if not stat.S_ISREG(s.st_mode) or stat.S_IMODE(s.st_mode) & 0o077 or s.st_size > 1024 * 1024:
                _fail("unsafe_database_metadata_file", "Database metadata must be a private bounded regular JSON file")
            return json.loads(stream.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeError):
        _fail("invalid_database_metadata_file", "The database metadata JSON file could not be decoded")


def legacy_manifest(config, root: Path) -> dict:
    """Validate a legacy SHA256SUMS/native-PG export; return engine metadata.

    Old dumps do not record installed extension versions in CREATE EXTENSION.
    Rather than inventing evidence, require LEGACY_DATABASE_METADATA_FILE: a
    private JSON list with the same per-DB metadata as a modern engine export.
    This adapter never writes a manifest or modifies the legacy archives.
    """
    root = _directory(root)
    if config.engine != "postgres":
        _fail("legacy_native_unsupported", "Only PostgreSQL custom-format legacy native exports are supported")
    names = _names(config)
    supplied = _private_json(config, "LEGACY_DATABASE_METADATA_FILE")
    if isinstance(supplied, dict):
        supplied = supplied.get("databases")
    if not isinstance(supplied, list) or [d.get("name") for d in supplied if isinstance(d, dict)] != names:
        _fail("database_set_mismatch", "Legacy database metadata must exactly match configured databases")
    checksums = _archive(root, "SHA256SUMS")
    if checksums.stat().st_size > 1024 * 1024:
        _fail("invalid_legacy_checksums", "Legacy checksum metadata is too large")
    try:
        lines = checksums.read_text(encoding="utf-8").splitlines()
    except UnicodeError:
        _fail("invalid_legacy_checksums", "Legacy checksum metadata is not UTF-8")
    verified = {}
    for line in lines:
        m = re.fullmatch(r"([0-9a-fA-F]{64}) [ *]([^\r\n]+)", line)
        if not m or m.group(2) in verified:
            _fail("invalid_legacy_checksums", "Legacy checksums must list unique safe relative filenames")
        path = _archive(root, m.group(2))
        with path.open("rb") as f:
            digest = hashlib.file_digest(f, "sha256").hexdigest()
        if not hmac.compare_digest(digest, m.group(1).lower()):
            _fail("legacy_checksum_mismatch", "A legacy native archive checksum does not match")
        verified[m.group(2)] = path
    dumps = []
    for filename, path in verified.items():
        with path.open("rb") as f:
            if f.read(5) == b"PGDMP":
                dumps.append(filename)
    if len(dumps) != len(names):
        _fail("legacy_archive_set_mismatch", "Legacy custom archive count does not match configured databases")
    globals_name = _env(config).get("LEGACY_GLOBALS_FILE")
    if not globals_name:
        options = [name for name in verified if name.startswith("globals") and name.endswith(".sql")]
        if len(options) != 1:
            _fail("legacy_globals_required", "Legacy globals filename must be explicitly configured or unambiguous")
        globals_name = options[0]
    if globals_name not in verified:
        _fail("legacy_globals_required", "Legacy globals must be covered by the checksum list")
    globfile = verified[globals_name]
    if globfile.stat().st_size > 8 * 1024 * 1024:
        _fail("invalid_legacy_globals", "Legacy globals exceed the bounded role-metadata limit")
    try:
        globals_sql = globfile.read_text(encoding="utf-8")
    except UnicodeError:
        _fail("invalid_legacy_globals", "Legacy globals are not UTF-8")
    uncommented = "\n".join(x for x in globals_sql.splitlines() if not x.lstrip().startswith("--"))
    if re.search(r"\bPASSWORD\s+(?:'|NULL|E')", uncommented, re.IGNORECASE):
        _fail("legacy_password_artifact", "Legacy globals must omit password fields")
    token = r'(?:"(?:[^"]|"")+"|[a-zA-Z_][a-zA-Z_0-9$]*)'
    login_roles = []
    for match in re.finditer(r"(?m)^ALTER ROLE (" + token + r") WITH ([^;]+);", globals_sql):
        if re.search(r"\bLOGIN\b", match.group(2)):
            raw = match.group(1)
            login_roles.append(raw[1:-1].replace('""', '"') if raw.startswith('"') else raw)
    mapped = {}
    source = None
    target = pg_probe_major(config)
    for filename in dumps:
        archive = verified[filename]
        toc = _call(config, [_pg_bin(config, target, "pg_restore"), "--list", str(archive)]).stdout.decode("utf-8", "strict")
        version = re.search(r"(?m)^;\s*Dumped from database version:\s*(\d+)\.", toc)
        client = re.search(r"(?m)^;\s*Dumped by pg_dump version:\s*(\d+)\.", toc)
        db = re.search(r"(?m)^;\s*dbname:\s*(.+)$", toc)
        if not version or not client or not db or db.group(1).strip() not in names:
            _fail("legacy_header_invalid", "Legacy archive version or database identity is incomplete")
        dbname = db.group(1).strip()
        major = _pg_major(config, version.group(1))
        if source is not None and source != version.group(1):
            _fail("legacy_source_mismatch", "Legacy custom archives have different source server major versions")
        source = version.group(1)
        if int(client.group(1)) > int(major) or dbname in mapped:
            _fail("legacy_header_invalid", "Legacy archive client version or duplicate database is unsupported")
        _call(config, [_pg_bin(config, major, "pg_restore"), "--exit-on-error", "--file=/dev/null", str(archive)])
        item = dict(supplied[names.index(dbname)])
        item["archive"] = filename
        # Metadata is operator-declared because custom archives omit extension
        # versions. This is not relabeled as measured source metadata.
        for key in ("encoding", "lc_collate", "lc_ctype", "locale_provider", "extensions"):
            if key not in item:
                _fail("legacy_metadata_required", "Legacy database locale or extension metadata is incomplete")
        item.setdefault("icu_locale", None)
        item.setdefault("collation_version", None)
        mapped[dbname] = item
    if set(mapped) != set(names) or source is None:
        _fail("legacy_archive_set_mismatch", "Legacy native archive database set does not match configuration")
    info = {"engine": "postgres", "source_major": source, "target_major": _pg_major(config, source),
            "legacy_native": True, "databases": [mapped[n] for n in names], "globals": globals_name,
            "login_roles": login_roles, "globals_contains_auth_hash": False,
            "metadata_evidence": "operator-declared-locale-extensions;native-header-and-SHA256-verified",
            "consistency": "legacy-native-custom-archive;globals-separate;source-consistency-not-inferred"}
    _check_pg_requirements(config, info["target_major"], info["databases"])
    return info
