#!/usr/bin/env python3
"""Explicitly opted-in, synthetic-only real CLI/restic/native-server E2E.

No image pull, build, socket mount, production environment forwarding or push.
Without --runtime-reviewed and --image this exits 2 and reports NOT RUN.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid


class E2EFailure(Exception):
    pass


def require(condition, code):
    if not condition:
        raise E2EFailure(code)


def clean_env():
    # Never inherit AWS, restic, database, proxy, Kubernetes or host credentials.
    return {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "PYTHONPATH": "/opt/k3s-restic", "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "TZ": "UTC",
            "HOME": "/tmp", "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt"}


def command(argv, *, env=None, data=None, ok=True, timeout=180):
    try:
        p = subprocess.Popen([str(x) for x in argv], env=env or clean_env(),
                             stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            stdout, stderr = p.communicate(data, timeout=timeout)
        except BaseException:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait()
            raise
    except (OSError, subprocess.TimeoutExpired):
        raise E2EFailure("subprocess_unavailable_or_timeout") from None
    # Raw native diagnostics/SQL/argv/env are intentionally not emitted.
    result = subprocess.CompletedProcess(argv, p.returncode, stdout, stderr)
    if ok and result.returncode:
        tool = Path(str(argv[0])).name
        if tool == "runuser" and "--" in argv:
            tool = Path(str(argv[argv.index("--") + 1])).name
        # Safe diagnostic categories only; never echo native stderr, SQL or env.
        category = "native_error"
        for token, label in ((b"permission denied", "permission_denied"),
                             (b"could not extend", "storage_error"), (b"no space left", "no_space"),
                             (b"does not exist", "missing_object"), (b"already exists", "object_exists"),
                             (b"syntax error", "syntax_error"), (b"read-only", "read_only"),
                             (b"cannot run inside a transaction", "transaction_block"),
                             (b"could not resize", "shared_memory_error")):
            if token in result.stderr.lower():
                category = label
                break
        raise E2EFailure("subprocess_" + tool + "_rc_" + str(result.returncode) + "_" + category)
    return result


def db_command(user, argv):
    return ["/usr/sbin/runuser", "--user", user, "--", *[str(x) for x in argv]]


def cli(env, action, expect_failure=False):
    argv = [sys.executable, "-m", "k3s_backup", action]
    if env.get("E2E_SAFE_TRACE") == "1":
        wrapper = "import sys,pathlib,re\nfrom k3s_backup import engines\nnative=engines.run\ndef traced(argv,**kwargs):\n check=kwargs.pop('check',True)\n result=native(argv,check=False,**kwargs)\n frames=[]\n f=sys._getframe(1)\n while f:\n  if pathlib.Path(f.f_code.co_filename).name=='engines.py':frames.append(f.f_code.co_name+':'+str(f.f_lineno))\n  f=f.f_back\n print('DIAG tool='+pathlib.Path(argv[0]).name+'_rc='+str(result.returncode)+'_frames='+','.join(frames),file=sys.stderr)\n if result.returncode:\n  for number in re.findall(rb'(?:ERROR|Error) +(\\d+)|\\((\\d{4,5})\\)',result.stderr):print('DIAG db_errno='+b''.join(number).decode(),file=sys.stderr)\n if check and result.returncode:\n  from k3s_backup.errors import BackupError\n  raise BackupError('command_failed','Synthetic native command failed')\n return result\nengines.run=traced\nfrom k3s_backup.__main__ import main\nsys.exit(main())"
        argv = [sys.executable, "-c", wrapper, action]
    r = command(argv, env=env, ok=False, timeout=240)
    if env.get("E2E_SAFE_TRACE") == "1":
        print(r.stderr.decode(errors="replace"), file=sys.stderr, end="")
    if r.returncode and not expect_failure:
        try:
            safe_code = json.loads(r.stdout.splitlines()[-1]).get("code", "unknown")
        except (ValueError, IndexError, AttributeError):
            safe_code = "invalid_result"
        if not isinstance(safe_code, str) or re.fullmatch(r"[a-z_]{1,80}", safe_code) is None:
            safe_code = "invalid_error_code"
        raise E2EFailure("cli_" + action + "_" + safe_code)
    if expect_failure:
        require(r.returncode != 0, "failed_export_reported_success")
        return None
    try:
        result = json.loads(r.stdout.splitlines()[-1])
    except (ValueError, IndexError):
        raise E2EFailure("cli_result_missing") from None
    require(result.get("event") == action, "unexpected_cli_event")
    return result


def base_env(work, target, repo, case):
    e = clean_env()
    e.update(RESTIC_REPOSITORY=str(repo), RESTIC_PASSWORD="synthetic-repository-only",
             RESTIC_BIN="/usr/local/bin/restic", REPOSITORY_ISOLATED="true",
             DATASET_ID="container-e2e-" + case, DATA_PATH=str(target), BACKUP_WORK_PATH=str(work),
             BACKUP_MIN_FREE_BYTES="1048576", BACKUP_MAX_BYTES="268435456",
             JOB_TIMEOUT_SECONDS="180", RESTORE_MAX_AGE_SECONDS="0", HOSTNAME="synthetic-old-pod")
    return e


def init_repo(env):
    command(["restic", "--no-cache", "init"], env=env)


def snapshots(env):
    return json.loads(command(["restic", "--no-cache", "snapshots", "--json"], env=env).stdout)


def check_repo(env):
    command(["restic", "--no-cache", "check", "--read-data"], env=env)


def reject_failed_export(env):
    before = snapshots(env)
    success = (Path(env["BACKUP_WORK_PATH"]) / "status/last_success.json").read_bytes()
    broken = dict(env, DATABASE_HOST="/tmp/does-not-exist-e2e.sock", MARIADB_SOCKET="/tmp/does-not-exist-e2e.sock")
    cli(broken, "backup", expect_failure=True)
    require(snapshots(env) == before, "failed_export_created_snapshot")
    require((Path(env["BACKUP_WORK_PATH"]) / "status/last_success.json").read_bytes() == success,
            "failed_export_replaced_last_success")


@contextmanager
def case_paths(case):
    # Only these newly created synthetic directories are ever removed.
    root = Path(tempfile.mkdtemp(prefix="e2e-" + case + "-", dir="/tmp"))
    work = Path("/backup") / ("e2e-" + case)
    repo = Path("/backup") / ("repo-" + case)
    target = Path("/data") / ("e2e-" + case)
    created = [root]
    try:
        for p in (work, target):
            p.mkdir(mode=0o700)
            created.append(p)
        require(not repo.exists(), "synthetic_repository_already_exists")
        created.append(repo)
        yield root, work, target, repo
    finally:
        for p in reversed(created):
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)


def files_e2e():
    with case_paths("files") as (root, work, target, repo):
        source = root / "source"
        source.mkdir()
        payload = source / "payload.bin"
        payload.write_bytes(bytes(range(256)) * 16)
        payload.chmod(0o640)
        (source / "nested").mkdir(mode=0o750)
        (source / "nested/link").symlink_to("../payload.bin")
        env = base_env(work, source, repo, "files")
        init_repo(env)
        require(cli(env, "backup").get("status") == "backed_up", "file_backup_failed")
        payload.write_bytes(b"latest-synthetic-file\0" + bytes(range(256)))
        expected = hashlib.sha256(payload.read_bytes()).hexdigest()
        require(cli(env, "backup").get("status") == "backed_up", "latest_file_backup_failed")
        require(len(snapshots(env)) == 2, "file_snapshot_count")
        recovery = dict(env, DATA_PATH=str(target), HOSTNAME="synthetic-recreated-pod")
        result = cli(recovery, "restore-init")
        require(result.get("status") == "restored", "file_restore_not_completed")
        restored = target / "payload.bin"
        require(hashlib.sha256(restored.read_bytes()).hexdigest() == expected, "latest_file_content_mismatch")
        require(stat.S_IMODE(restored.stat().st_mode) == 0o640, "file_mode_mismatch")
        require((target / "nested/link").is_symlink(), "symlink_not_preserved")
        require(os.readlink(target / "nested/link") == "../payload.bin", "symlink_target_mismatch")
        previous = restored.read_bytes()
        cli(recovery, "restore-init")
        require(restored.read_bytes() == previous, "nonempty_restart_overwrote_data")
        check_repo(env)
    print("PASS files: latest snapshot, recreated Pod identity, mode/symlink, nonempty restart, restic full check")


@contextmanager
def pg_server(major, data, socket_dir, *, initialize=False, uid=999, gid=999):
    bindir = Path("/usr/lib/postgresql") / str(major) / "bin"
    socket_dir.mkdir(mode=0o700)
    os.chown(socket_dir, uid, gid)
    as_owner = lambda argv: db_command("postgres", argv) if (uid, gid) == (999, 999) else ["/usr/bin/setpriv", "--reuid=" + str(uid), "--regid=" + str(gid), "--clear-groups", "--", *[str(x) for x in argv]]
    if initialize:
        print("RUN pg" + str(major) + ": initdb", flush=True)
        data.mkdir(mode=0o700)
        require((uid, gid) == (999, 999), "fixture_init_requires_packaged_account")
        os.chown(data, uid, gid)
        command(as_owner([bindir / "initdb", "--pgdata", data, "--username=postgres",
                                         "--encoding=UTF8", "--locale=en_US.UTF-8", "--auth=trust"]))
    options = ("-c listen_addresses='' -c unix_socket_directories=" + str(socket_dir)
               + " -c shared_buffers=16MB -c max_connections=10 -c logging_collector=off -c max_wal_senders=3 -c jit=off")
    log = socket_dir / "server.log"
    started = False
    try:
        print("RUN pg" + str(major) + ": server-start", flush=True)
        command(as_owner([bindir / "pg_ctl", "--pgdata", data, "--wait", "--timeout=60",
                                         "--log", log, "--options", options, "start"]))
        started = True
        yield bindir
    finally:
        if started:
            command(as_owner([bindir / "pg_ctl", "--pgdata", data, "--wait", "--timeout=60",
                                             "--mode=fast", "stop"]))
            require(not (data / "postmaster.pid").exists(), "postgres_unclean_shutdown")


def pg_sql(bindir, socket_dir, database, sql, password):
    env = dict(clean_env(), PGPASSWORD=password)
    return command([bindir / "psql", "--no-psqlrc", "--no-password", "--host", socket_dir,
                    "--username=postgres", "--dbname", database, "--tuples-only", "--no-align", "--set=ON_ERROR_STOP=1"],
                   env=env, data=sql.encode()).stdout.strip()


def postgres_e2e(major, *, physical=False, old_vector=False, dual=False, final_uid=999):
    case = "pg" + str(major) + ("-uid70" if final_uid == 70 else "-dual" if dual else "-physical" if physical else "-vector082" if old_vector else "")
    password = "synthetic-database-only"
    with case_paths(case) as (root, work, target, repo):
        os.chown(root, 999, 999)
        source = root / "source"
        socket_dir = root / "socket-source"
        env = base_env(work, target, repo, case)
        env.update(DATABASE_TYPE="postgres", DATABASE_HOST=str(socket_dir), DATABASE_USER="postgres",
                   DATABASE_PASSWORD=password, DATABASE_NAME="fixture_a,fixture_b", DATABASE_MAJOR=str(major),
                   DB_UID="999", DB_GID="999")
        if physical or dual:
            env["BACKUP_MODE"] = "dual" if dual else "physical"
        baseline = {}
        query = """SELECT json_build_object('rows',(SELECT json_agg(row_to_json(t) ORDER BY id) FROM
          (SELECT id,body,encode(bytes,'hex') AS bytes,embedding::text FROM app.items) t),
          'encoding',current_setting('server_encoding'),
          'locale',(SELECT datcollate FROM pg_database WHERE datname=current_database()),
          'owner',(SELECT tableowner FROM pg_tables WHERE schemaname='app' AND tablename='items'),
          'reader',has_table_privilege('fixture_reader','app.items','SELECT'),
          'vector',(SELECT extversion FROM pg_extension WHERE extname='vector'),
          'fk',(SELECT count(*) FROM pg_constraint WHERE contype='f' AND conrelid='app.children'::regclass),
          'indexes',(SELECT count(*) FROM pg_indexes WHERE schemaname='app'),
          'sequence',(SELECT last_value FROM app.items_id_seq));"""
        with pg_server(major, source, socket_dir, initialize=True) as bindir:
            print("RUN " + case + ": bootstrap-roles-databases", flush=True)
            pg_sql(bindir, socket_dir, "postgres", "ALTER ROLE postgres PASSWORD '" + password + "'; "
                   "CREATE ROLE fixture_owner NOLOGIN; CREATE ROLE fixture_reader NOLOGIN; "
                   "CREATE DATABASE fixture_a OWNER fixture_owner; CREATE DATABASE fixture_b OWNER fixture_owner;", password)
            for database in ("fixture_a", "fixture_b"):
                print("RUN " + case + ": synthetic-schema-and-baseline", flush=True)
                extension = "CREATE EXTENSION vector VERSION '0.8.2';" if old_vector else "CREATE EXTENSION vector;"
                pg_sql(bindir, socket_dir, database, extension + """
                  CREATE SCHEMA app AUTHORIZATION fixture_owner;
                  SET ROLE fixture_owner;
                  CREATE TABLE app.items(id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    body jsonb NOT NULL, bytes bytea, embedding vector(3));
                  INSERT INTO app.items(body,bytes,embedding) VALUES
                    ('{"fixture":"UTF8-中文","ok":true}',decode('00ff1234','hex'),'[1,2,3]'),
                    ('{"fixture":"second"}',decode('aaff','hex'),'[4,5,6]');
                  CREATE TABLE app.children(id bigint PRIMARY KEY, item_id bigint REFERENCES app.items(id));
                  INSERT INTO app.children VALUES (1,1);
                  CREATE INDEX fixture_vector_idx ON app.items USING hnsw(embedding vector_l2_ops);
                  CREATE VIEW app.summary AS SELECT id,body FROM app.items;
                  GRANT USAGE ON SCHEMA app TO fixture_reader;
                  GRANT SELECT ON ALL TABLES IN SCHEMA app TO fixture_reader;
                  RESET ROLE;""", password)
                baseline[database] = pg_sql(bindir, socket_dir, database, query, password)
            print("RUN " + case + ": restic-init-and-backup", flush=True)
            init_repo(env)
            backup_result = cli(env, "backup")
            require(backup_result.get("status") == "backed_up", "postgres_backup_failed")
            if dual:
                require(set(backup_result.get("streams", {})) == {"logical", "physical"}, "dual_stream_missing")
                require(len(snapshots(env)) == 2, "dual_snapshot_count_mismatch")
            reject_failed_export(env)
        recovery = dict(env, HOSTNAME="synthetic-recreated-pod", DB_UID=str(final_uid), DB_GID=str(final_uid))
        if final_uid != 999:
            # No passwd entry is added: initdb must run as the packaged helper,
            # then the clean, validated tree must be handed to the target UID.
            import pwd
            try: pwd.getpwuid(final_uid)
            except KeyError: pass
            else: raise E2EFailure("target_uid_should_have_no_packaged_account")
            os.chmod(root, 0o711)
        print("RUN " + case + ": restore-init", flush=True)
        require(cli(recovery, "restore-init").get("status") == "restored", "postgres_restore_not_completed")
        require((target / "PG_VERSION").read_text().strip() == str(major), "postgres_target_major_mismatch")
        hba = (target / "pg_hba.conf").read_text()
        require("scram-sha-256" in hba, "postgres_final_hba_not_scram")
        # The published root may be a mountpoint; its ownership must permit the real DB uid.
        require(target.stat().st_uid == final_uid and target.stat().st_gid == final_uid, "postgres_published_root_uid")
        for parent, dirs, files in os.walk(target):
            for entry in dirs + files:
                s = (Path(parent) / entry).lstat()
                if parent == str(target) and entry == ".k3s-restic-restore.json":
                    # The publication journal is deliberately owned by the root runner,
                    # not the database process. It is not native PostgreSQL payload.
                    require(s.st_uid == 0 and stat.S_IMODE(s.st_mode) == 0o600, "postgres_restore_journal_owner")
                    continue
                require((s.st_uid, s.st_gid) == (final_uid, final_uid), "postgres_published_tree_uid")
        with pg_server(major, target, root / "socket-restored", uid=final_uid, gid=final_uid) as bindir:
            for database in ("fixture_a", "fixture_b"):
                actual = pg_sql(bindir, root / "socket-restored", database, query, password)
                require(actual == baseline[database], "postgres_restored_invariants_mismatch")
                pg_sql(bindir, root / "socket-restored", database,
                       "INSERT INTO app.items(body,embedding) VALUES ('{}','[7,8,9]');", password)
        check_repo(env)
    print("PASS " + case + ": " + ("native logical+physical dual stream" if dual else "pg_basebackup+pg_verifybackup+WAL replay" if physical else "native archives+globals/roles") + ", two DBs, vector/JSONB/bytea/FK/index/sequence/locale, published restart, clean shutdown")


def maria_owner_command(argv, uid=998, gid=998):
    require(type(uid) is int and type(gid) is int and 0 < uid < 2**32 - 1 and 0 < gid < 2**32 - 1,
            "mariadb_fixture_invalid_numeric_identity")
    if (uid, gid) == (998, 998):
        return db_command("mysql", argv)
    return ["/usr/bin/setpriv", "--reuid=" + str(uid), "--regid=" + str(gid),
            "--clear-groups", "--", *[str(item) for item in argv]]


def maria_runtime(root, name, uid, gid):
    path = root / name
    path.mkdir(mode=0o700)
    os.chown(path, uid, gid)
    return path


def maria_published_owner(target, uid, gid):
    require((target.stat().st_uid, target.stat().st_gid) == (uid, gid), "mariadb_published_root_identity")
    for path in target.rglob("*"):
        info = path.lstat()
        if path.parent == target and path.name == ".k3s-restic-restore.json":
            require(info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600, "mariadb_restore_journal_owner")
            continue
        require((info.st_uid, info.st_gid) == (uid, gid), "mariadb_published_tree_identity")


@contextmanager
def maria_server(data, socket_path, root, *, initialize=False, password="", uid=998, gid=998):
    if initialize:
        data.mkdir(mode=0o700)
        os.chown(data, uid, gid)
        command(maria_owner_command(["mariadb-install-db", "--no-defaults", "--datadir=" + str(data),
                                     "--auth-root-authentication-method=normal", "--skip-test-db",
                                     "--innodb-log-file-size=8M"], uid, gid))
    logfile = root / "mariadb-native.log"
    stream = logfile.open("ab")
    proc = None
    try:
        argv = maria_owner_command(["mariadbd", "--no-defaults", "--datadir=" + str(data),
                   "--socket=" + str(socket_path), "--pid-file=" + str(root / "maria.pid"),
                   "--tmpdir=" + str(root), "--skip-networking", "--skip-log-bin", "--skip-name-resolve",
                   "--innodb-buffer-pool-size=32M", "--innodb-log-file-size=8M", "--innodb-use-native-aio=0"], uid, gid)
        proc = subprocess.Popen(argv, env=clean_env(), stdin=subprocess.DEVNULL, stdout=stream,
                                stderr=stream, start_new_session=True)
        deadline = time.monotonic() + 60
        while True:
            require(proc.poll() is None, "mariadb_server_exited_before_ready")
            if socket_path.exists():
                r = maria_sql(socket_path, "SELECT 1;", "" if initialize else password, ok=False)
                if r.returncode == 0:
                    break
            require(time.monotonic() < deadline, "mariadb_readiness_timeout")
            time.sleep(0.1)
        if initialize:
            # Match a deliberate application deployment, not install-db test accounts.
            # MariaDB11.8 --system=users upstream errors on leftover anonymous grants.
            maria_sql(socket_path, "DROP USER IF EXISTS ''@'localhost'; DROP USER IF EXISTS ''@'" + os.uname().nodename + "'; DROP USER IF EXISTS 'root'@'" + os.uname().nodename + "';", "")
        if initialize and password:
            maria_sql(socket_path, "ALTER USER 'root'@'localhost' IDENTIFIED BY '" + password + "';", "")
        yield
    finally:
        if proc is not None and proc.poll() is None:
            env = dict(clean_env(), MYSQL_PWD=password)
            r = command(["mariadb-admin", "--no-defaults", "--protocol=SOCKET", "--socket=" + str(socket_path),
                         "--user=root", "shutdown"], env=env, ok=False, timeout=30)
            if r.returncode:
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                raise E2EFailure("mariadb_unclean_shutdown") from None
            require(r.returncode == 0 and proc.returncode == 0, "mariadb_shutdown_failed")
        stream.close()


def maria_sql(socket_path, sql, password, ok=True):
    return command(["mariadb", "--no-defaults", "--protocol=SOCKET", "--socket=" + str(socket_path),
                    "--user=root", "--batch", "--skip-column-names"],
                   env=dict(clean_env(), MYSQL_PWD=password), data=sql.encode(), ok=ok)


def maria_auth_state(socket_path, password):
    """Keep synthetic auth/grants only in memory; never print verifiers or SQL."""
    from k3s_backup.engines import _maria_account
    raw = maria_sql(socket_path, "SELECT COALESCE(JSON_ARRAYAGG(JSON_OBJECT('user',User,'host',Host)),JSON_ARRAY()) FROM mysql.user WHERE is_role='N';", password).stdout
    accounts = json.loads(raw)
    state = {}
    for account in accounts:
        user, host = account["user"], account["host"]
        literal = _maria_account(user, host)
        account_sql = maria_sql(socket_path, "SET sql_mode=''; SHOW CREATE USER " + literal + ";", password).stdout
        grants = maria_sql(socket_path, "SET sql_mode=''; SHOW GRANTS FOR " + literal + ";", password).stdout.splitlines()
        state[(user, host)] = {"authentication": account_sql, "grants": tuple(sorted(grants))}
    return state


def mariadb_e2e(final_uid=998):
    password = "synthetic-database-only"
    case = "mariadb" if final_uid == 998 else "maria-final-uid" + str(final_uid)
    if final_uid == 999:
        require(pwd.getpwuid(999).pw_name == "postgres", "mariadb_uid999_nss_fixture_changed")
    with case_paths(case) as (root, work, target, repo):
        # Traverse only: private socket/tmp directories have their own DB UID.
        root.chmod(0o711)
        source_runtime = maria_runtime(root, "source-runtime", 998, 998)
        source, source_socket = root / "source", source_runtime / "server.sock"
        env = base_env(work, target, repo, case)
        env.update(DATABASE_TYPE="mariadb", DATABASE_HOST=str(source_socket), MARIADB_SOCKET=str(source_socket),
                   DATABASE_USER="root", DATABASE_PASSWORD=password, DATABASE_NAME="fixture_a,fixture_b",
                   DATABASE_MAJOR="11.8", DB_UID=str(final_uid), DB_GID=str(final_uid))
        baseline = {}
        def query(database):
            return ("SELECT id,JSON_COMPACT(body),HEX(bytes),note FROM " + database + ".items ORDER BY id;"
                    "SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA='" + database + "';"
                    "SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA='" + database + "';"
                    "SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA='" + database + "';"
                    "SELECT COUNT(*) FROM mysql.user WHERE User='fixture_reader';")
        with maria_server(source, source_socket, source_runtime, initialize=True, password=password):
            from k3s_backup.engines import _maria_hash
            maria_sql(source_socket, "CREATE USER 'fixture_reader'@'localhost';"
                      "CREATE USER 'fixture_multi'@'localhost' IDENTIFIED VIA mysql_native_password USING '" + _maria_hash("synthetic-local-only") + "';"
                      "CREATE USER 'fixture_multi'@'%' IDENTIFIED VIA mysql_native_password USING '" + _maria_hash("synthetic-remote-only") + "';"
                      "CREATE USER 'root'@'%' IDENTIFIED VIA mysql_native_password USING '" + _maria_hash("synthetic-distinct-root-remote") + "';"
                      "CREATE USER 'fixture_socket'@'localhost' IDENTIFIED VIA unix_socket;", password)
            for database in ("fixture_a", "fixture_b"):
                maria_sql(source_socket, "CREATE DATABASE " + database + " CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;"
                   "CREATE TABLE " + database + ".items(id bigint AUTO_INCREMENT PRIMARY KEY,body JSON,bytes BLOB,note VARCHAR(20)) ENGINE=InnoDB;"
                   "CREATE TRIGGER " + database + ".upper_note BEFORE INSERT ON " + database + ".items FOR EACH ROW SET NEW.note=UPPER(NEW.note);"
                   "INSERT INTO " + database + ".items(body,bytes,note) VALUES ('{\"fixture\":\"中文\"}',UNHEX('00ff1234'),'first'),('{}',UNHEX('aaff'),'second');"
                   "CREATE TABLE " + database + ".children(id bigint PRIMARY KEY,item_id bigint,FOREIGN KEY(item_id) REFERENCES items(id)) ENGINE=InnoDB;"
                   "INSERT INTO " + database + ".children VALUES(1,1);"
                   "CREATE VIEW " + database + ".summary AS SELECT id,body FROM " + database + ".items;"
                   "CREATE PROCEDURE " + database + ".fixture_count() SELECT COUNT(*) FROM " + database + ".items;"
                   "CREATE EVENT " + database + ".fixture_event ON SCHEDULE EVERY 1 DAY DISABLE DO SELECT 1;"
                   "GRANT SELECT ON " + database + ".* TO 'fixture_reader'@'localhost';"
                   "GRANT SELECT ON " + database + ".* TO 'fixture_multi'@'localhost';"
                   "GRANT SELECT,INSERT ON " + database + ".* TO 'fixture_multi'@'%';", password)
                baseline[database] = maria_sql(source_socket, query(database), password).stdout
            if env.get("E2E_SAFE_TRACE") == "1":
                accounts = json.loads(maria_sql(source_socket, "SELECT JSON_ARRAYAGG(JSON_OBJECT('user',User,'host',Host,'role',is_role)) FROM mysql.user;", password).stdout)
                for ordinal, account in enumerate(accounts):
                    literal = "'" + account["user"].replace("'", "''") + "'@'" + account["host"].replace("'", "''") + "'"
                    for operation in ("SHOW CREATE USER ", "SHOW GRANTS FOR "):
                        test = maria_sql(source_socket, operation + literal + ";", password, ok=False)
                        if test.returncode:
                            known = account["user"] if account["user"] in {"root", "mysql", "mariadb.sys", "mysql.sys", "PUBLIC", "fixture_reader", ""} else "other"
                            print("DIAG account_ordinal=" + str(ordinal) + "_known=" + known + "_role=" + account["role"] + "_op=" + operation.split()[1] + "_rc=" + str(test.returncode), flush=True)
            auth_baseline = maria_auth_state(source_socket, password)
            local_grants_query = "SELECT * FROM mysql.db WHERE User='fixture_multi' AND Host='localhost' ORDER BY Db;"
            local_grants = maria_sql(source_socket, local_grants_query, password).stdout
            require(auth_baseline[("fixture_multi", "localhost")]["authentication"] != auth_baseline[("fixture_multi", "%")]["authentication"], "mariadb_fixture_host_auth_not_distinct")
            init_repo(env)
            require(cli(env, "backup").get("status") == "backed_up", "mariadb_backup_failed")
            reject_failed_export(env)
        require(cli(dict(env, HOSTNAME="synthetic-recreated-pod"), "restore-init").get("status") == "restored",
                "mariadb_restore_not_completed")
        maria_published_owner(target, final_uid, final_uid)
        restored_runtime = maria_runtime(root, "restored-runtime", final_uid, final_uid)
        restored_socket = restored_runtime / "server.sock"
        with maria_server(target, restored_socket, restored_runtime, password=password, uid=final_uid, gid=final_uid):
            require(maria_auth_state(restored_socket, password) == auth_baseline, "mariadb_default_auth_or_grants_changed")
            for database in ("fixture_a", "fixture_b"):
                actual = maria_sql(restored_socket, query(database), password).stdout
                require(actual == baseline[database], "mariadb_restored_invariants_mismatch")
                maria_sql(restored_socket, "INSERT INTO " + database + ".items(body,note) VALUES('{}','third');", password)
        # Restore the SAME uploaded snapshot into a second empty target with one
        # explicit account override. Source/restored verifier bytes never print.
        override_target = target.parent / (target.name + "-auth-override")
        require(not override_target.exists(), "mariadb_override_target_already_exists")
        override_target.mkdir(mode=0o700)
        try:
            mapping = root / "synthetic-account-overrides.json"
            mapping.write_text(json.dumps({"schema_version": 1, "accounts": [
                {"user": "fixture_multi", "host": "localhost", "plugin": "mysql_native_password",
                 "password": "synthetic-explicit-local-only"}]}))
            mapping.chmod(0o600)
            override_env = dict(env, DATA_PATH=str(override_target), HOSTNAME="synthetic-override-pod",
                                RESTORE_MARIADB_ACCOUNT_PASSWORDS_FILE=str(mapping))
            require(cli(override_env, "restore-init").get("status") == "restored", "mariadb_explicit_override_restore_failed")
            maria_published_owner(override_target, final_uid, final_uid)
            override_runtime = maria_runtime(root, "override-runtime", final_uid, final_uid)
            override_socket = override_runtime / "server.sock"
            with maria_server(override_target, override_socket, override_runtime, password=password, uid=final_uid, gid=final_uid):
                changed = maria_auth_state(override_socket, password)
                key = ("fixture_multi", "localhost")
                require(set(changed) == set(auth_baseline), "mariadb_override_changed_account_set")
                for account in changed:
                    if account == key:
                        require(changed[account]["authentication"] != auth_baseline[account]["authentication"], "mariadb_requested_override_not_changed")
                    else:
                        require(changed[account] == auth_baseline[account], "mariadb_override_changed_unmapped_account")
                # SHOW GRANTS can include the intentionally changed verifier;
                # the fixture's per-schema grants must still be identical.
                require(maria_sql(override_socket, local_grants_query, password).stdout == local_grants, "mariadb_override_changed_schema_grants")
                actual_hash = maria_sql(override_socket, "SELECT authentication_string FROM mysql.user WHERE User='fixture_multi' AND Host='localhost';", password).stdout.strip()
                require(actual_hash == _maria_hash("synthetic-explicit-local-only").encode(), "mariadb_override_verifier_mismatch")
        finally:
            if override_target.is_dir() and not override_target.is_symlink():
                shutil.rmtree(override_target)
        check_repo(env)
        if final_uid == 999:
            require(pwd.getpwuid(999).pw_name == "postgres", "mariadb_uid999_nss_mapping_modified")
    print("PASS " + case + ": source uid998, restored uid" + str(final_uid) + ", preserved per-host auth/plugins/grants, explicit single-host override, native dumps, published restart, clean shutdown")


def container_args(image, name, runtime, case):
    return ["create", "--name", name, "--pull=never", "--runtime=" + runtime,
            "--network=none", "--read-only", "--cap-drop=ALL", "--cap-add=CHOWN",
            "--cap-add=SETUID", "--cap-add=SETGID", "--cap-add=DAC_OVERRIDE", "--cap-add=FOWNER",
            "--security-opt=no-new-privileges:true", "--memory=768m", "--memory-swap=768m",
            "--cpus=0.5", "--pids-limit=192", "--ipc=private", "--shm-size=32m",
            "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=512m,mode=1777",
            "--tmpfs=/backup:rw,nosuid,nodev,noexec,size=384m,mode=0700",
            "--tmpfs=/data:rw,nosuid,nodev,noexec,size=384m,mode=0700",
            "--env=E2E_SYNTHETIC=1", "--entrypoint=python3", image,
            "/opt/k3s-restic/tests/container_e2e.py", "--inside", "--case", case]


def inside(case):
    require(os.environ.get("E2E_SYNTHETIC") == "1", "synthetic_opt_in_missing")
    require(Path("/.dockerenv").exists(), "container_boundary_missing")
    require(os.geteuid() == 0, "fixture_needs_root_to_drop_db_uid")
    # Defense in depth: do not accept a default network route or docker/kube socket.
    routes = Path("/proc/net/route").read_text().splitlines()[1:]
    require(not any(len(r.split()) > 1 and r.split()[1] == "00000000" for r in routes), "default_route_present")
    require(not Path("/var/run/docker.sock").exists(), "docker_socket_present")
    os.umask(0o077)
    # Mount roots need traversal by the dedicated database uid, never world writes.
    os.chmod("/data", 0o711)
    command(["image-selftest"])
    cases = {"files": files_e2e, "pg16": lambda: postgres_e2e(16),
             "pg17": lambda: postgres_e2e(17), "pg17-uid70": lambda: postgres_e2e(17, final_uid=70), "pg17-vector082": lambda: postgres_e2e(17, old_vector=True),
             "pg17-physical": lambda: postgres_e2e(17, physical=True), "pg17-dual": lambda: postgres_e2e(17, dual=True), "mariadb": mariadb_e2e,
             "maria-final-uid999": lambda: mariadb_e2e(final_uid=999)}
    for name in cases if case == "all" else [case]:
        cases[name]()
    print("PASS requested synthetic container E2E; no production compatibility claim")


class HarnessTests(unittest.TestCase):
    def test_run_is_isolated_and_never_pulls(self):
        argv = container_args("sha256:" + "a" * 64, "fixture", "patched", "all")
        for flag in ("--pull=never", "--network=none", "--read-only", "--cap-drop=ALL", "--runtime=patched"):
            self.assertIn(flag, argv)
        self.assertFalse(any(x.startswith(("-v", "--volume", "--mount")) for x in argv))
        self.assertFalse(any("docker.sock" in x for x in argv))

    def test_does_not_forward_host_credentials(self):
        self.assertFalse(any(k.startswith(("AWS", "RESTIC", "DATABASE", "KUBE")) for k in clean_env()))

    def test_maria_uid999_uses_numeric_identity_not_packaged_mysql(self):
        argv = maria_owner_command(["mariadbd", "--no-defaults"], 999, 999)
        self.assertEqual(argv, ["/usr/bin/setpriv", "--reuid=999", "--regid=999", "--clear-groups", "--", "mariadbd", "--no-defaults"])
        self.assertNotIn("mysql", argv)
        self.assertNotIn("postgres", argv)
        self.assertEqual(maria_owner_command(["mariadbd"]), db_command("mysql", ["mariadbd"]))
        for uid in (0, -1, True, "999", 2**32 - 1):
            with self.subTest(uid=uid), self.assertRaises(E2EFailure):
                maria_owner_command(["mariadbd"], uid, 999)

    def test_maria_uid999_install_and_restart_commands_agree_without_native_execution(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-maria-uid-unit-") as directory:
            root = Path(directory)
            data, socket_path = root / "data", root / "server.sock"
            socket_path.touch()
            proc = mock.Mock()
            proc.poll.return_value = None
            proc.returncode = 0
            proc.wait.return_value = 0
            native = sys.modules[__name__]
            with mock.patch.object(native, "command", return_value=subprocess.CompletedProcess([], 0, b"", b"")) as called:
                with mock.patch.object(native, "maria_sql", return_value=subprocess.CompletedProcess([], 0, b"1", b"")):
                    with mock.patch.object(subprocess, "Popen", return_value=proc) as start:
                        with mock.patch.object(os, "chown") as owner:
                            with maria_server(data, socket_path, root, initialize=True, uid=999, gid=999):
                                pass
            owner.assert_called_once_with(data, 999, 999)
            install = called.call_args_list[0].args[0]
            server = start.call_args.args[0]
            for argv in (install, server):
                self.assertEqual(argv[:5], ["/usr/bin/setpriv", "--reuid=999", "--regid=999", "--clear-groups", "--"])
            self.assertIn("mariadb-install-db", install)
            self.assertIn("mariadbd", server)
            self.assertIn("--skip-networking", server)
            self.assertIn("--auth-root-authentication-method=normal", install)
            proc.wait.assert_called_once_with(timeout=30)

    def test_maria_published_owner_checks_tree_and_exempts_private_journal(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-maria-owner-unit-") as directory:
            root = Path(directory)
            payload = root / "payload"
            payload.write_bytes(b"synthetic")
            marker = root / ".k3s-restic-restore.json"
            marker.write_bytes(b"{}")
            marker.chmod(0o600)
            real_stat, real_lstat = Path.stat, Path.lstat
            def mapped(path, *, follow_symlinks=True):
                result = real_stat(path, follow_symlinks=follow_symlinks)
                if path == marker:
                    return result
                values = list(result)
                values[4] = values[5] = 999
                return os.stat_result(values)
            def mapped_link(path):
                return mapped(path, follow_symlinks=False)
            with mock.patch.object(Path, "stat", mapped), mock.patch.object(Path, "lstat", mapped_link):
                maria_published_owner(root, 999, 999)
            with self.assertRaises(E2EFailure):
                maria_published_owner(root, 999, 999)

    def test_maria_uid999_cli_case_dispatches_without_container_execution(self):
        with mock.patch.object(sys.modules[__name__], "inside") as run:
            self.assertEqual(main(["--inside", "--case", "maria-final-uid999"]), 0)
            run.assert_called_once_with("maria-final-uid999")

    def test_distinct_scratch_and_restore_mounts(self):
        argv = container_args("image", "fixture", "runc", "files")
        for mount in ("/tmp:", "/backup:", "/data:"):
            self.assertEqual(1, sum(x.startswith("--tmpfs=" + mount) for x in argv))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image")
    parser.add_argument("--docker-host", help="Explicit reviewed daemon unix:// socket; no implicit local daemon")
    parser.add_argument("--runtime", default="runc")
    parser.add_argument("--runtime-reviewed", action="store_true")
    parser.add_argument("--inside", action="store_true")
    parser.add_argument("--case", choices=("all", "files", "pg16", "pg17", "pg17-uid70", "pg17-vector082", "pg17-physical", "pg17-dual", "mariadb", "maria-final-uid999"), default="all")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--profile", choices=("lite", "all"), help="Test only scenarios shipped by this image label")
    args = parser.parse_args(argv)
    if args.profile:
        require(args.case == "all", "profile_cannot_be_combined_with_explicit_case")
        cases = {"lite": ["files"], "all": ["all"]}[args.profile]
        if args.inside:
            for case in cases: inside(case)
            return 0
        for case in cases:
            flags = ["--runtime-reviewed"] if args.runtime_reviewed else []
            rc = main(["--image", args.image or "", "--docker-host", args.docker_host or "", "--runtime", args.runtime, "--case", case, *flags])
            if rc: return rc
        return 0
    if args.self_test:
        return 0 if unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(HarnessTests)).wasSuccessful() else 1
    if args.inside:
        inside(args.case)
        return 0
    if not args.runtime_reviewed or not args.image or not args.docker_host:
        print("NOT RUN: supply --runtime-reviewed, --image and an explicitly reviewed --docker-host", file=sys.stderr)
        return 2
    require(args.docker_host.startswith("unix:///"), "only_explicit_local_unix_daemon_allowed")
    require(re.fullmatch(r"[A-Za-z0-9_.-]+", args.runtime) is not None, "invalid_runtime_name")
    docker = shutil.which("docker")
    require(docker is not None, "docker_client_unavailable")
    with tempfile.TemporaryDirectory(prefix="backup-e2e-client-") as scratch:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": scratch, "DOCKER_CONFIG": scratch}
        prefix = [docker, "--host", args.docker_host]
        # Pin execution to an already present image ID; there is deliberately no pull fallback.
        image = command([*prefix, "image", "inspect", "--format={{.Id}}", args.image], env=env).stdout.decode().strip()
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", image) is not None, "local_image_id_missing")
        name = "backup-e2e-" + uuid.uuid4().hex[:12]
        created = False
        try:
            command([*prefix, *container_args(image, name, args.runtime, args.case)], env=env)
            created = True
            result = command([*prefix, "start", "--attach", name], env=env, ok=False, timeout=1200)
            # Child only emits safe stage names/assertion codes, never native errors or SQL.
            print(result.stdout.decode(errors="replace"), end="")
            print(result.stderr.decode(errors="replace"), file=sys.stderr, end="")
            status = command([*prefix, "inspect", "--format={{.State.ExitCode}}", name], env=env).stdout.decode().strip()
            require(result.returncode == 0 and status == "0", "container_e2e_failed")
        finally:
            if created:
                command([*prefix, "rm", "--force", "--volumes", name], env=env)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except E2EFailure as exc:
        print("FAIL synthetic E2E: " + str(exc), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print("FAIL synthetic E2E: internal_harness_error (raw diagnostics suppressed)", file=sys.stderr)
        sys.exit(1)
