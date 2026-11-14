"""Synthetic tests only: no server, container, network or real secrets."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings

from k3s_backup import engines
from k3s_backup.errors import BackupError
from k3s_backup.process import Result


def pg_db(name="alpha", archive="db-0000.dump"):
    return {"name": name, "archive": archive, "encoding": "UTF8",
            "lc_collate": "en_US.UTF-8", "lc_ctype": "en_US.UTF-8",
            "locale_provider": "c", "icu_locale": None, "collation_version": "2.36",
            "extensions": [{"name": "plpgsql", "version": "1.0"}, {"name": "vector", "version": "0.8.7"}]}


def pg_info(names=("alpha",)):
    return {"engine": "postgres", "source_major": "17", "target_major": "17",
            "databases": [pg_db(n, "db-%04d.dump" % i) for i, n in enumerate(names)],
            "globals": "globals.sql", "login_roles": ["app_owner"]}


def maria_info(names=("alpha", "beta")):
    return {"engine": "mariadb", "source_major": "11.8", "target_major": "11.8",
            "databases": [{"name": n, "archive": "databases.sql", "character_set": "utf8mb4", "collation": "utf8mb4_unicode_ci"} for n in names],
            "globals": "globals.sql", "login_accounts": [{"user": "app_owner", "host": "%", "global_grant_option": False}], "proxy_grants": []}


class EnginesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-engines-")
        self.root = Path(self.tmp.name)
        self.stage = self.root / "input"
        self.stage.mkdir(mode=0o700)
        self.target = self.root / "output"
        self.target.mkdir(mode=0o700)
        self.cfg = SimpleNamespace(engine="postgres", databases=["alpha"], db_host="synthetic.invalid", db_port="5432",
                                   db_user="app_owner", db_password="synthetic-password-only", target_major=None,
                                   run_timeout=5, env={}, dataset_id="synthetic", work_path=self.root)
        self.calls = []
        self.share = self.root / "share" / "extension"
        self.share.mkdir(parents=True)
        for name, version in (("plpgsql", "1.0"), ("vector", "0.8.7")):
            (self.share / (name + ".control")).write_text("default_version = '" + version + "'\n")
        self.cfg.env["PG_SHARE_DIR_17"] = str(self.share.parent)
        self.identity_patcher = patch.object(engines, "_pg_restore_identities", return_value=((os.geteuid(), os.getegid()), (os.geteuid(), os.getegid())))
        self.identity_patcher.start()

    def tearDown(self):
        self.identity_patcher.stop()
        self.tmp.cleanup()

    def error(self, code, callback):
        with self.assertRaises(BackupError) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)
        self.assertNotIn(self.cfg.db_password, str(caught.exception))

    def fake_run(self, argv, **kw):
        self.assertTrue(kw.get("check"))
        self.calls.append((list(argv), dict(kw)))
        tool = Path(argv[0]).name
        if kw.get("stdout_path"):
            kw["stdout_path"].write_bytes(b"native synthetic archive\n")
        if tool == "locale":
            return Result(0, b"C\nC.utf8\nen_US.utf8\n")
        if tool == "initdb":
            dest = Path(argv[argv.index("-D") + 1])
            for n in ("pg_hba.conf", "postgresql.conf", "PG_VERSION"):
                (dest / n).write_text("17\n" if n == "PG_VERSION" else "# synthetic\n")
        if tool == "pg_restore" and "--list" in argv:
            header = b"; dbname: alpha\n; Dumped from database version: 17.11\n; Dumped by pg_dump version: 17.11\n"
            # Native --list filters DATABASE entries unless --create is set.
            database = b"2; 1262 16384 DATABASE - alpha app_owner\n3; 0 0 DATABASE PROPERTIES - alpha app_owner\n" if "--create" in argv else b""
            return Result(0, header + database + b"4; 1259 1 TABLE public t app_owner\n")
        if tool == "pg_controldata":
            return Result(0, b"Database cluster state:               shut down\n")
        if tool == "psql":
            sql = kw.get("input_data", b"").decode()
            if "server_version_num" in sql:
                return Result(0, b"170011\n")
            if "json_agg(rolname" in sql:
                return Result(0, b'["app_owner"]\n')
            if "json_build_object" in sql:
                conn = kw["env"]["PGDATABASE"]
                name = next(n for n in self.cfg.databases if "dbname='" + n + "'" == conn)
                d = pg_db(name, "ignored")
                d["external_tablespaces"] = 0
                return Result(0, json.dumps(d).encode())
            if "pg_is_in_recovery()" in sql:
                return Result(0, b"f\n")
            if "count(*) FROM pg_tablespace" in sql:
                return Result(0, b"0\n")
        if tool == "mariadbd":
            return Result(0, b"mariadbd Ver 11.8.9-MariaDB for debian-linux-gnu\n")
        if tool in {"mariadb", "mariadb-dump"}:
            opt = next((a.split("=", 1)[1] for a in argv if a.startswith("--defaults-file=")), None)
            if opt:
                self.assertEqual(0o600, stat.S_IMODE(Path(opt).stat().st_mode))
                self.assertFalse(Path(opt).is_relative_to(self.stage))
            sql = kw.get("input_data", b"").decode()
            if "SELECT VERSION()" in sql:
                return Result(0, b"11.8.9-MariaDB-deb12\n")
            if "FROM mysql.proxies_priv" in sql and "JSON_ARRAYAGG" in sql:
                return Result(0, b"[]\n")
            if "SELECT IF(COUNT(*)=" in sql:
                return Result(0, b"1\n")
            if "COUNT(*)" in sql:
                return Result(0, b"0\n")
            if "global_grant_option" in sql and "JSON_ARRAYAGG" in sql:
                return Result(0, json.dumps(getattr(self, "maria_login_state", maria_info()["login_accounts"])).encode())
            if "JSON_ARRAYAGG" in sql:
                return Result(0, json.dumps(maria_info()["login_accounts"]).encode())
            if "JSON_OBJECT('name'" in sql:
                n = next(n for n in self.cfg.databases if n.encode().hex() in sql)
                d = next(d for d in maria_info(self.cfg.databases)["databases"] if d["name"] == n)
                return Result(0, json.dumps(d).encode())
        return Result(0)

    def payload(self, info):
        if info.get("engine") == "mariadb":
            self.maria_login_state = info["login_accounts"]
        for filename in {d["archive"] for d in info["databases"]} | {info["globals"]}:
            (self.stage / filename).write_bytes(b"synthetic-only\n")

    @contextmanager
    def access(self, config, target, engine, *, identity=None):
        yield identity if identity is not None else (os.geteuid(), os.getegid())

    @contextmanager
    def server(self, *args, **kwargs):
        self.calls.append((["server-start"], {}))
        try:
            yield self.root / "fake.sock"
        finally:
            self.calls.append((["server-stopped"], {}))

    def test_pg_exports_every_database_and_decodes_all_blocks(self):
        self.cfg.databases = ["alpha", "beta"]
        with patch.object(engines, "run", self.fake_run):
            info = engines.export_database(self.cfg, self.stage)
        self.assertEqual(["alpha", "beta"], [d["name"] for d in info["databases"]])
        self.assertEqual(["db-0000.dump", "db-0001.dump"], [d["archive"] for d in info["databases"]])
        self.assertFalse((self.stage / "backup-manifest.json").exists())
        dumps = [a for a, _ in self.calls if Path(a[0]).name == "pg_dump"]
        self.assertEqual(2, len(dumps))
        self.assertTrue(all("--format=custom" in a and "--create" in a for a in dumps))
        decoded = [a for a, _ in self.calls if Path(a[0]).name == "pg_restore"]
        self.assertEqual(2, len(decoded))
        self.assertTrue(all("--file=/dev/null" in a and "--exit-on-error" in a for a in decoded))
        glob = next(a for a, _ in self.calls if Path(a[0]).name == "pg_dumpall")
        self.assertIn("--no-role-passwords", glob)
        for a, _ in self.calls:
            self.assertNotIn(self.cfg.db_password, " ".join(a))
        self.assertNotIn(self.cfg.db_password, json.dumps(info))

    def test_pg_nonzero_export_never_succeeds(self):
        def broken(argv, **kw):
            if Path(argv[0]).name == "pg_dump":
                return Result(9, stderr=b"synthetic sensitive diagnostic")
            return self.fake_run(argv, **kw)
        with patch.object(engines, "run", broken):
            self.error("database_command_failed", lambda: engines.export_database(self.cfg, self.stage))
        self.assertFalse((self.stage / "globals.sql").exists())

    def test_pg_decode_failure_is_not_a_valid_export(self):
        def broken(argv, **kw):
            if Path(argv[0]).name == "pg_restore":
                raise BackupError("command_failed", "Native decode failed")
            return self.fake_run(argv, **kw)
        with patch.object(engines, "run", broken):
            self.error("command_failed", lambda: engines.export_database(self.cfg, self.stage))

    def test_pg_migrations_explicit_and_not_downgrades(self):
        self.error("unsupported_postgres_version", lambda: engines._pg_major(self.cfg, "14"))
        self.cfg.target_major = "17"
        self.assertEqual("17", engines._pg_major(self.cfg, "14"))
        self.cfg.target_major = "16"
        self.error("unsupported_postgres_version", lambda: engines._pg_major(self.cfg, "17"))

    def test_conninfo_database_name_is_safely_quoted(self):
        conn = engines._pg_conn(self.cfg, "name' host=evil\\x")
        self.assertEqual("dbname='name\\' host=evil\\\\x'", conn["PGDATABASE"])
        self.assertEqual(self.cfg.db_password, conn["PGPASSWORD"])

    def test_empty_stage_and_system_database_guards(self):
        self.cfg.databases = ["alpha", "alpha"]
        self.error("invalid_databases", lambda: engines.export_database(self.cfg, self.stage))
        self.cfg.databases = ["template1"]
        self.error("system_database", lambda: engines.export_database(self.cfg, self.stage))
        (self.stage / "unrelated").write_text("preserve")
        self.error("database_target_not_empty", lambda: engines.export_database(self.cfg, self.stage))

    def test_archive_rejects_traversal_symlink_hardlink_empty(self):
        self.error("invalid_database_archive", lambda: engines._archive(self.stage, "../outside"))
        (self.root / "outside").write_bytes(b"test")
        (self.stage / "link").symlink_to(self.root / "outside")
        self.error("invalid_database_archive", lambda: engines._archive(self.stage, "link"))
        (self.stage / "zero").touch()
        self.error("invalid_database_archive", lambda: engines._archive(self.stage, "zero"))
        os.link(self.root / "outside", self.stage / "hardlink")
        self.error("invalid_database_archive", lambda: engines._archive(self.stage, "hardlink"))

    def test_pg_restore_preserves_owners_and_only_verifier_not_plaintext(self):
        info = pg_info()
        self.payload(info)
        with patch.object(engines, "run", self.fake_run), patch.object(engines, "_database_access", self.access), patch.object(engines, "_pg_server", self.server):
            engines.restore_database(self.cfg, self.stage, self.target, {"engine": info})
        restore = next(a for a, _ in self.calls if Path(a[0]).name == "pg_restore" and "--create" in a and "--list" not in a)
        self.assertIn("--create", restore)
        self.assertNotIn("--no-owner", restore)
        self.assertNotIn("--no-privileges", restore)
        sql = b"\n".join(k.get("input_data", b"") for _, k in self.calls)
        self.assertIn(b"SCRAM-SHA-256$4096", sql)
        self.assertNotIn(self.cfg.db_password.encode(), sql)
        hba = (self.target / "pg_hba.conf").read_text()
        self.assertFalse(any(line.strip().endswith(" trust") for line in hba.splitlines() if not line.startswith("#")))
        self.assertIn("0.0.0.0/0 scram-sha-256", hba)
        self.assertEqual("server-stopped", self.calls[-1][0][0])

    def test_pg_restore_failure_always_stops_server(self):
        info = pg_info()
        self.payload(info)
        def broken(argv, **kw):
            if Path(argv[0]).name == "pg_restore":
                raise BackupError("command_failed", "Restore failed safely")
            return self.fake_run(argv, **kw)
        with patch.object(engines, "run", broken), patch.object(engines, "_database_access", self.access), patch.object(engines, "_pg_server", self.server):
            self.error("command_failed", lambda: engines.restore_database(self.cfg, self.stage, self.target, {"engine": info}))
        self.assertEqual("server-stopped", self.calls[-1][0][0])

    def test_locale_and_extension_fail_closed(self):
        with patch.object(engines, "run", self.fake_run):
            d = pg_db()
            d["lc_collate"] = "missing.UTF-8"
            self.error("locale_missing", lambda: engines._check_pg_requirements(self.cfg, "17", [d]))
            d = pg_db()
            d["extensions"][1]["version"] = "0.7.0"
            self.error("unsupported_extension_version", lambda: engines._check_pg_requirements(self.cfg, "17", [d]))
            d["extensions"][1]["name"] = "unknown"
            self.error("unsupported_extension", lambda: engines._check_pg_requirements(self.cfg, "17", [d]))
            d["locale_provider"] = "b"
            self.error("unsupported_locale", lambda: engines._check_pg_requirements(self.cfg, "17", [d]))

    def test_restore_preserves_nonempty_target_and_checks_db_set(self):
        info = pg_info()
        self.payload(info)
        info["databases"][0]["name"] = "other"
        self.error("database_set_mismatch", lambda: engines.restore_database(self.cfg, self.stage, self.target, {"engine": info}))
        (self.target / "unrelated").write_text("do not overwrite")
        self.error("database_target_not_empty", lambda: engines.restore_database(self.cfg, self.stage, self.target, {"engine": info}))
        self.assertEqual("do not overwrite", (self.target / "unrelated").read_text())

    def test_role_password_mapping_private_and_no_unknown_roles(self):
        mapping = self.root / "passwords.json"
        mapping.write_text(json.dumps({"extra": "synthetic-other"}))
        mapping.chmod(0o644)
        self.cfg.env["RESTORE_ROLE_PASSWORDS_FILE"] = str(mapping)
        self.error("unsafe_role_password_file", lambda: engines._role_passwords(self.cfg))
        mapping.chmod(0o600)
        self.assertEqual("synthetic-other", engines._role_passwords(self.cfg)["extra"])
        info = pg_info()
        self.payload(info)
        with patch.object(engines, "run", self.fake_run):
            self.error("unknown_restore_role", lambda: engines.restore_database(self.cfg, self.stage, self.target, {"engine": info}))

    def test_missing_role_password_safe_warning(self):
        info = pg_info()
        info["login_roles"].append("other_role_not_for_logs")
        self.payload(info)
        with patch.object(engines, "run", self.fake_run), patch.object(engines, "_database_access", self.access), patch.object(engines, "_pg_server", self.server), warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            engines.restore_database(self.cfg, self.stage, self.target, {"engine": info})
        self.assertIn("restore_role_passwords_missing", str(caught[0].message))
        self.assertNotIn("other_role_not_for_logs", str(caught[0].message))

    def test_uid_override_no_passwd_entry_and_never_root(self):
        self.cfg.env.update(DB_UID="12345", DB_GID="23456")
        with patch.object(engines.os, "geteuid", return_value=0), patch.object(engines.pwd, "getpwnam", side_effect=AssertionError("no lookup")):
            self.assertEqual((12345, 23456), engines._identity(self.cfg, "postgres"))
            a = engines._as_database(self.cfg, ["/server"], (12345, 23456), parent_death=True)
            self.assertIn("--pdeathsig=TERM", a)
            self.assertIn("--clear-groups", a)
        self.cfg.env.update(DB_UID="0", DB_GID="1")
        self.error("database_identity", lambda: engines._identity(self.cfg, "postgres"))

    def test_owned_stage_and_mount_modes_restored_after_failure(self):
        volume = self.root / "volume"
        volume.mkdir(mode=0o700)
        parent = volume / (".k3s-restic-stage-" + "a" * 32)
        parent.mkdir(mode=0o700)
        payload = parent / "payload"
        payload.mkdir()
        with patch.object(engines, "_identity", return_value=(os.geteuid(), os.getegid())):
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                with engines._database_access(self.cfg, payload, "postgres"):
                    self.assertEqual(0o710, stat.S_IMODE(parent.stat().st_mode))
                    self.assertEqual(0o710, stat.S_IMODE(volume.stat().st_mode))
                    raise RuntimeError("synthetic")
        self.assertEqual(0o700, stat.S_IMODE(parent.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(volume.stat().st_mode))

    def test_maria_one_all_database_transaction_and_private_options(self):
        self.cfg.engine = "mariadb"
        self.cfg.databases = ["alpha", "beta"]
        with patch.object(engines, "run", self.fake_run):
            info = engines.export_database(self.cfg, self.stage)
        dumps = [a for a, _ in self.calls if Path(a[0]).name == "mariadb-dump"]
        self.assertEqual(2, len(dumps))
        glob, data = dumps
        self.assertIn("--system=users", glob)
        self.assertIn("--replace", glob)
        for flag in ("--single-transaction", "--routines", "--events", "--triggers", "--hex-blob", "--dump-history"):
            self.assertIn(flag, data)
        self.assertEqual(["alpha", "beta"], data[-2:])
        self.assertTrue(info["globals_contains_auth_hash"])
        for a, _ in self.calls:
            self.assertNotIn(self.cfg.db_password, " ".join(a))
        for a in dumps:
            option = next(x.split("=", 1)[1] for x in a if x.startswith("--defaults-file="))
            self.assertFalse(Path(option).exists())

    def test_maria_refuses_noninnodb(self):
        self.cfg.engine = "mariadb"
        def noninnodb(argv, **kw):
            if b"COUNT(*)" in kw.get("input_data", b""):
                return Result(0, b"1\n")
            return self.fake_run(argv, **kw)
        with patch.object(engines, "run", noninnodb):
            self.error("nontransactional_mariadb_tables", lambda: engines.export_database(self.cfg, self.stage))
        self.assertFalse(any(self.stage.iterdir()))

    def test_maria_no_implicit_upgrade_or_mysql_claim(self):
        self.cfg.engine = "mariadb"
        self.error("unsupported_mariadb_version", lambda: engines._maria_target(self.cfg, "11.5"))
        self.error("unsupported_mariadb_version", lambda: engines._maria_version("8.4.7 MySQL Community"))
        self.cfg.target_major = "11.8"
        with patch.object(engines, "run", self.fake_run):
            self.assertEqual("11.8", engines._maria_target(self.cfg, "11.5"))

    def test_maria_options_escape_socket_and_cleanup(self):
        self.cfg.db_password = 'synthetic"quote\\slash\nnewline'
        self.cfg.db_host = "/tmp/synthetic.sock"
        with engines._runtime_dir((os.geteuid(), os.getegid())) as runtime:
            with engines._maria_options(self.cfg, runtime) as path:
                data = path.read_text()
                self.assertIn('password="synthetic\\"quote\\\\slash\\nnewline"', data)
                self.assertIn("protocol=socket", data)
                self.assertNotIn("protocol=tcp", data)
            self.assertFalse(path.exists())
        self.assertFalse(runtime.exists())

    def test_maria_restore_streams_archives_without_connection_password_rewrite(self):
        self.cfg.engine = "mariadb"
        self.cfg.databases = ["alpha", "beta"]
        info = maria_info()
        self.payload(info)
        with patch.object(engines, "run", self.fake_run), patch.object(engines, "_database_access", self.access), patch.object(engines, "_maria_server", self.server):
            engines.restore_database(self.cfg, self.stage, self.target, {"engine": info})
        streams = [k["input_path"].name for _, k in self.calls if k.get("input_path")]
        self.assertEqual(["globals.sql", "databases.sql"], streams)
        sql = b"\n".join(k.get("input_data", b"") for _, k in self.calls)
        self.assertNotIn(self.cfg.db_password.encode(), sql)
        self.assertNotIn(engines._maria_hash(self.cfg.db_password).encode(), sql)
        self.assertNotIn(b"ALTER USER", sql)
        self.assertIn(b"DROP USER", sql)
        self.assertEqual("server-stopped", self.calls[-1][0][0])

    def maria_restore(self, info=None, runner=None):
        self.cfg.engine = "mariadb"
        self.cfg.databases = ["alpha", "beta"]
        info = maria_info() if info is None else info
        if not (self.stage / info["globals"]).exists():
            self.payload(info)
        with patch.object(engines, "run", runner or self.fake_run), patch.object(engines, "_database_access", self.access), patch.object(engines, "_maria_server", self.server):
            engines.restore_database(self.cfg, self.stage, self.target, {"engine": info})

    def maria_mapping(self, records, *, mode=0o600):
        path = self.root / "synthetic-maria-map.json"
        path.write_text(json.dumps({"schema_version": 1, "accounts": records}))
        path.chmod(mode)
        self.cfg.env["RESTORE_MARIADB_ACCOUNT_PASSWORDS_FILE"] = str(path)
        return path

    def test_maria_default_preserves_same_user_multiple_host_native_globals(self):
        info = maria_info()
        info["login_accounts"] = [{"user": "app_owner", "host": "localhost", "global_grant_option": False},
                                   {"user": "app_owner", "host": "%", "global_grant_option": False},
                                   {"user": "socket_user", "host": "localhost", "global_grant_option": False},
                                   {"user": "pam_user", "host": "%", "global_grant_option": False}]
        self.payload(info)
        globals_sql = ("CREATE USER 'app_owner'@'localhost' IDENTIFIED VIA mysql_native_password USING '*SYNTHETIC_ONE';\n"
                       "CREATE USER 'app_owner'@'%' IDENTIFIED VIA mysql_native_password USING '*SYNTHETIC_TWO';\n"
                       "CREATE USER 'socket_user'@'localhost' IDENTIFIED VIA unix_socket;\n"
                       "CREATE USER 'pam_user'@'%' IDENTIFIED VIA pam USING 'synthetic-policy';\n"
                       "GRANT SELECT ON alpha.* TO 'app_owner'@'localhost';\n")
        (self.stage / "globals.sql").write_text(globals_sql)
        self.maria_restore(info)
        self.assertEqual((self.stage / "globals.sql").read_text(), globals_sql)
        sql = b"\n".join(k.get("input_data", b"") for _, k in self.calls)
        self.assertNotIn(b"ALTER USER", sql)
        self.assertNotIn(engines._maria_hash(self.cfg.db_password).encode(), sql)
        self.assertEqual([k["input_path"] for _, k in self.calls if k.get("input_path")][0], self.stage / "globals.sql")

    def test_maria_explicit_override_matches_only_exact_host_and_hash_no_plaintext(self):
        info = maria_info()
        info["login_accounts"] = [{"user": "app_owner", "host": "localhost", "global_grant_option": False},
                                   {"user": "app_owner", "host": "%", "global_grant_option": False}]
        self.maria_mapping([{"user": "app_owner", "host": "localhost", "plugin": "mysql_native_password", "password": "synthetic-specific-host"}])
        def runner(argv, **kwargs):
            result = self.fake_run(argv, **kwargs)
            if b"SELECT COUNT(*) FROM mysql.user WHERE BINARY" in kwargs.get("input_data", b""):
                return Result(0, b"1\n")
            return result
        self.maria_restore(info, runner)
        sql = b"\n".join(k.get("input_data", b"") for _, k in self.calls)
        self.assertIn(b"ALTER USER 'app_owner'@'localhost'", sql)
        self.assertNotIn(b"ALTER USER 'app_owner'@'%'", sql)
        self.assertIn(engines._maria_hash("synthetic-specific-host").encode(), sql)
        self.assertNotIn(b"synthetic-specific-host", sql)
        self.assertNotIn(engines._maria_hash(self.cfg.db_password).encode(), sql)
        self.assertEqual("server-stopped", self.calls[-1][0][0])

    def test_maria_refuses_role_only_mapping_without_reading_it(self):
        self.cfg.env["RESTORE_ROLE_PASSWORDS_FILE"] = "/synthetic-path-not-read"
        self.error("ambiguous_mariadb_password_mapping", self.maria_restore)
        self.assertFalse(any(argv == ["server-start"] for argv, _ in self.calls))

    def test_maria_mapping_private_file_modes_symlink_hardlink_and_owner(self):
        entries = [{"user": "app_owner", "host": "%", "plugin": "mysql_native_password", "password": "synthetic-other"}]
        for mode in (0o644, 0o640, 0o400, 0o700):
            with self.subTest(mode=mode):
                self.maria_mapping(entries, mode=mode)
                self.error("unsafe_mariadb_password_file", lambda: engines._maria_account_passwords(self.cfg))
        path = self.maria_mapping(entries)
        alias = self.root / "mapping-link"
        alias.symlink_to(path)
        self.cfg.env["RESTORE_MARIADB_ACCOUNT_PASSWORDS_FILE"] = str(alias)
        self.error("invalid_mariadb_password_file", lambda: engines._maria_account_passwords(self.cfg))
        alias.unlink()
        os.link(path, alias)
        self.cfg.env["RESTORE_MARIADB_ACCOUNT_PASSWORDS_FILE"] = str(path)
        self.error("unsafe_mariadb_password_file", lambda: engines._maria_account_passwords(self.cfg))
        alias.unlink()
        if os.geteuid() == 0:
            os.chown(path, 65534, 65534)
            self.error("unsafe_mariadb_password_file", lambda: engines._maria_account_passwords(self.cfg))

    def test_maria_mapping_invalid_missing_host_duplicate_identity_and_json(self):
        valid = {"user": "app_owner", "host": "%", "plugin": "mysql_native_password", "password": "synthetic-other"}
        for entries in ([{key: value for key, value in valid.items() if key != "host"}], [valid, valid],
                        [{**valid, "password": ""}], [{**valid, "host": ""}], [{**valid, "user": "bad\0user"}],
                        [{**valid, "password": "bad\0password"}], [{**valid, "user": "\ud800"}], [{**valid, "password": "\ud800"}]):
            with self.subTest(entries=entries):
                self.maria_mapping(entries)
                self.error("invalid_mariadb_password_file", lambda: engines._maria_account_passwords(self.cfg))
        path = self.maria_mapping([valid])
        for raw in ("{", "[]", '{"schema_version":1,"schema_version":1,"accounts":[]}'):
            path.write_text(raw)
            self.error("invalid_mariadb_password_file", lambda: engines._maria_account_passwords(self.cfg))
        self.maria_mapping([{**valid, "plugin": "pam"}])
        self.error("unsupported_mariadb_auth_override", lambda: engines._maria_account_passwords(self.cfg))

    def test_maria_absent_host_override_refused_before_server_start(self):
        self.maria_mapping([{"user": "app_owner", "host": "localhost", "plugin": "mysql_native_password", "password": "synthetic-other"}])
        self.error("unknown_restore_account", self.maria_restore)
        self.assertFalse(any(argv == ["server-start"] for argv, _ in self.calls))

    def test_maria_actual_imported_account_missing_and_permission_errors_are_fatal(self):
        self.maria_mapping([{"user": "app_owner", "host": "%", "plugin": "mysql_native_password", "password": "synthetic-other"}])
        self.error("unknown_restore_account", self.maria_restore)
        self.assertEqual("server-stopped", self.calls[-1][0][0])
        def denied(argv, **kwargs):
            if b"SELECT COUNT(*) FROM mysql.user WHERE BINARY" in kwargs.get("input_data", b""):
                raise BackupError("command_failed", "Synthetic account query denied")
            return self.fake_run(argv, **kwargs)
        self.error("command_failed", lambda: self.maria_restore(runner=denied))
        self.assertEqual("server-stopped", self.calls[-1][0][0])

    def test_maria_override_verification_failure_does_not_succeed(self):
        self.maria_mapping([{"user": "app_owner", "host": "%", "plugin": "mysql_native_password", "password": "synthetic-other"}])
        def mismatch(argv, **kwargs):
            result = self.fake_run(argv, **kwargs)
            sql = kwargs.get("input_data", b"")
            if b"SELECT COUNT(*) FROM mysql.user WHERE BINARY" in sql:
                return Result(0, b"0\n" if b"authentication_string" in sql else b"1\n")
            return result
        self.error("mariadb_auth_override_mismatch", lambda: self.maria_restore(runner=mismatch))
        self.assertEqual("server-stopped", self.calls[-1][0][0])

    def test_maria_connection_password_is_not_an_override_even_if_user_is_absent(self):
        self.cfg.db_user = "connection_only"
        self.maria_restore()
        sql = b"\n".join(k.get("input_data", b"") for _, k in self.calls)
        self.assertNotIn(b"ALTER USER", sql)
        self.assertNotIn(engines._maria_hash(self.cfg.db_password).encode(), sql)

    def test_maria_explicit_percent_host_is_literal_account_not_host_broadcast(self):
        self.maria_mapping([{"user": "app_owner", "host": "%", "plugin": "mysql_native_password", "password": "synthetic-percent-only"}])
        def runner(argv, **kwargs):
            result = self.fake_run(argv, **kwargs)
            if b"SELECT COUNT(*) FROM mysql.user WHERE BINARY" in kwargs.get("input_data", b""):
                return Result(0, b"1\n")
            return result
        self.maria_restore(runner=runner)
        sql = b"\n".join(k.get("input_data", b"") for _, k in self.calls)
        self.assertIn(b"ALTER USER 'app_owner'@'%'", sql)
        self.assertNotIn(b" LIKE ", sql)

    def test_maria_mapping_read_failure_and_fifo_are_rejected_without_blocking(self):
        self.cfg.env["RESTORE_MARIADB_ACCOUNT_PASSWORDS_FILE"] = str(self.root / "missing")
        self.error("invalid_mariadb_password_file", lambda: engines._maria_account_passwords(self.cfg))
        fifo = self.root / "mapping-fifo"
        os.mkfifo(fifo, 0o600)
        self.cfg.env["RESTORE_MARIADB_ACCOUNT_PASSWORDS_FILE"] = str(fifo)
        self.error("unsafe_mariadb_password_file", lambda: engines._maria_account_passwords(self.cfg))
        self.cfg.env["RESTORE_MARIADB_ACCOUNT_PASSWORDS_FILE"] = str(self.root / "denied")
        with patch.object(engines.os, "open", side_effect=PermissionError()):
            self.error("invalid_mariadb_password_file", lambda: engines._maria_account_passwords(self.cfg))

    def test_maria_explicit_alter_permission_failure_stops_without_success(self):
        self.maria_mapping([{"user": "app_owner", "host": "%", "plugin": "mysql_native_password", "password": "synthetic-other"}])
        def denied(argv, **kwargs):
            result = self.fake_run(argv, **kwargs)
            sql = kwargs.get("input_data", b"")
            if b"SELECT COUNT(*) FROM mysql.user WHERE BINARY" in sql:
                return Result(0, b"1\n")
            if b"ALTER USER" in sql:
                raise BackupError("command_failed", "Synthetic account update denied")
            return result
        self.error("command_failed", lambda: self.maria_restore(runner=denied))
        self.assertEqual("server-stopped", self.calls[-1][0][0])

    def test_maria_temporary_bootstrap_cannot_replace_source_account(self):
        info = maria_info()
        info["login_accounts"].append({"user": "k3s_restore_" + "a" * 16, "host": "localhost", "global_grant_option": True})
        with patch.object(engines.secrets, "token_hex", return_value="a" * 16):
            self.error("restore_bootstrap_collision", lambda: self.maria_restore(info))
        self.assertFalse(any(argv == ["server-start"] for argv, _ in self.calls))

    def test_maria_global_grant_metadata_missing_and_nonbool_fail_before_server(self):
        for value in ("missing", None, 0, 1, "N", "false", []):
            with self.subTest(value=value):
                info = maria_info()
                if value == "missing":
                    del info["login_accounts"][0]["global_grant_option"]
                    code = "mariadb_global_grant_metadata_required"
                else:
                    info["login_accounts"][0]["global_grant_option"] = value
                    code = "invalid_mariadb_login_metadata"
                self.error(code, lambda: self.maria_restore(info))
        self.assertFalse(any(argv == ["server-start"] for argv, _ in self.calls))

    def test_maria_global_grant_repair_is_exact_host_and_preserves_scope(self):
        accounts = [{"user": "same", "host": "localhost", "global_grant_option": False},
                    {"user": "same", "host": "%", "global_grant_option": True}]
        current = [{**item, "global_grant_option": True} for item in accounts]
        scope = {"db": [{"Grant_priv": "Y"}], "tables_priv": [{"Table_priv": "Select,Grant"}],
                 "columns_priv": [{"Column_priv": "Select"}], "procs_priv": [{"Proc_priv": "Execute,Alter Routine,Grant"}]}
        queries = []
        def query(config, options, sql):
            queries.append(sql)
            if "global_grant_option" in sql:
                return Result(0, json.dumps(current).encode())
            if sql.startswith("SELECT JSON_OBJECT("):
                return Result(0, json.dumps(scope).encode())
            if "REVOKE GRANT OPTION ON *.*" in sql:
                current[0]["global_grant_option"] = False
            return Result(0)
        with patch.object(engines, "_maria_query", side_effect=query):
            engines._maria_global_grant_sync(self.cfg, "unused", accounts)
        revoke = [sql for sql in queries if "REVOKE" in sql]
        self.assertEqual(revoke, ["SET sql_mode=''; REVOKE GRANT OPTION ON *.* FROM 'same'@'localhost'; FLUSH PRIVILEGES;"])
        self.assertEqual(current, accounts)
        self.assertNotIn("REVOKE ALL", "\n".join(queries))
        self.assertEqual(sum(sql.startswith("SELECT JSON_OBJECT(") for sql in queries), 2)

    def test_maria_required_global_grant_option_missing_never_implicitly_granted(self):
        accounts = [{"user": "same", "host": "%", "global_grant_option": True}]
        result = Result(0, json.dumps([{**accounts[0], "global_grant_option": False}]).encode())
        with patch.object(engines, "_maria_query", return_value=result) as query:
            self.error("mariadb_global_grant_restore_mismatch", lambda: engines._maria_global_grant_sync(self.cfg, "unused", accounts))
        self.assertFalse(any("GRANT OPTION ON" in call.args[2] for call in query.call_args_list))

    def test_maria_global_revoke_scope_loss_or_native_denial_is_fatal(self):
        accounts = [{"user": "same", "host": "localhost", "global_grant_option": False}]
        for cause in ("scope_loss", "denied", "still_true"):
            with self.subTest(cause=cause):
                changed = [False]
                def query(config, options, sql):
                    if "global_grant_option" in sql:
                        return Result(0, json.dumps([{**accounts[0], "global_grant_option": not changed[0] or cause == "still_true"}]).encode())
                    if sql.startswith("SELECT JSON_OBJECT("):
                        data = {"db": [], "tables_priv": [], "columns_priv": [],
                                "procs_priv": [{"Proc_priv": "Execute,Grant"}] if not changed[0] or cause != "scope_loss" else []}
                        return Result(0, json.dumps(data).encode())
                    if "REVOKE" in sql:
                        if cause == "denied":
                            raise BackupError("command_failed", "Synthetic revoke denied")
                        changed[0] = True
                    return Result(0)
                code = {"scope_loss": "mariadb_scoped_grant_restore_mismatch", "denied": "command_failed", "still_true": "mariadb_global_grant_restore_mismatch"}[cause]
                with patch.object(engines, "_maria_query", side_effect=query):
                    self.error(code, lambda: engines._maria_global_grant_sync(self.cfg, "unused", accounts))

    def test_maria_proxy_only_grant_option_repairs_global_and_rechecks_proxy(self):
        info = maria_info()
        info["proxy_grants"] = [{"user": "app_owner", "host": "%", "proxied_user": "", "proxied_host": "", "with_grant": True}]
        repaired = [False]
        def native(argv, **kwargs):
            result = self.fake_run(argv, **kwargs)
            sql = kwargs.get("input_data", b"")
            if b"global_grant_option" in sql:
                return Result(0, json.dumps([{**info["login_accounts"][0], "global_grant_option": not repaired[0]}]).encode())
            if sql.startswith(b"SELECT JSON_OBJECT(") and b"mysql.procs_priv" in sql:
                return Result(0, b'{"db":[],"tables_priv":[],"columns_priv":[],"procs_priv":[]}')
            if b"REVOKE GRANT OPTION ON *.*" in sql:
                repaired[0] = True
            if b"JSON_ARRAYAGG" in sql and b"mysql.proxies_priv" in sql:
                return Result(0, json.dumps(info["proxy_grants"]).encode())
            return result
        self.maria_restore(info, native)
        self.assertTrue(repaired[0])
        proxy_queries = [i for i, (_, kw) in enumerate(self.calls) if b"JSON_ARRAYAGG" in kw.get("input_data", b"") and b"mysql.proxies_priv" in kw.get("input_data", b"")]
        revoke_index = next(i for i, (_, kw) in enumerate(self.calls) if b"REVOKE GRANT OPTION" in kw.get("input_data", b""))
        self.assertGreater(proxy_queries[-1], revoke_index)
        self.assertEqual("server-stopped", self.calls[-1][0][0])

    def test_maria_global_grant_export_drift_is_not_accepted(self):
        self.cfg.engine, self.cfg.databases = "mariadb", ["alpha", "beta"]
        count = [0]
        def native(argv, **kwargs):
            result = self.fake_run(argv, **kwargs)
            if b"global_grant_option" in kwargs.get("input_data", b""):
                count[0] += 1
                return Result(0, json.dumps([{"user": "app_owner", "host": "%", "global_grant_option": count[0] == 2}]).encode())
            return result
        with patch.object(engines, "run", native):
            self.error("mariadb_accounts_changed_during_export", lambda: engines.export_database(self.cfg, self.stage))

    def test_maria_proxy_metadata_empty_host_percent_and_host_specific_preserved(self):
        accounts = [{"user": "root", "host": "localhost"}, {"user": "root", "host": "%"}]
        grants = [{"user": "root", "host": "localhost", "proxied_user": "", "proxied_host": "", "with_grant": True},
                  {"user": "root", "host": "%", "proxied_user": "", "proxied_host": "%", "with_grant": False}]
        actual = engines._maria_proxy_grants(grants, accounts)
        self.assertEqual({item["proxied_host"] for item in actual}, {"", "%"})
        self.assertEqual(actual, engines._maria_proxy_grants(list(reversed(grants)), accounts))
        sql_calls = []
        def query(config, options, sql):
            sql_calls.append(sql)
            if "JSON_ARRAYAGG" in sql:
                return Result(0, json.dumps(grants).encode())
            return Result(0)
        with patch.object(engines, "_maria_query", side_effect=query):
            engines._maria_proxy_sync(self.cfg, "unused", actual, accounts, "synthetic_bootstrap")
        self.assertTrue(sql_calls[0].startswith("DELETE FROM mysql.proxies_priv;INSERT INTO mysql.proxies_priv"))
        self.assertIn("CONVERT(X'' USING utf8mb4)", sql_calls[0])
        self.assertIn("CONVERT(X'25' USING utf8mb4)", sql_calls[0])
        self.assertIn("FLUSH PRIVILEGES", sql_calls[0])
        self.assertNotIn("synthetic_bootstrap", sql_calls[0])
        self.assertNotIn("UPDATE mysql.global_priv", sql_calls[0])

    def test_maria_proxy_metadata_missing_or_malformed_fails_before_server(self):
        info = maria_info()
        del info["proxy_grants"]
        self.error("mariadb_proxy_metadata_required", lambda: self.maria_restore(info))
        valid = {"user": "app_owner", "host": "%", "proxied_user": "", "proxied_host": "", "with_grant": True}
        for value in (None, {}, [valid, valid], [{**valid, "with_grant": 1}],
                      [{**valid, "with_grant": "1"}], [{**valid, "extra": "ignored"}],
                      [{key: value for key, value in valid.items() if key != "proxied_host"}],
                      [{**valid, "proxied_host": "bad\0host"}], [{**valid, "proxied_host": "\ud800"}]):
            with self.subTest(value=value):
                info = maria_info()
                info["proxy_grants"] = value
                self.error("invalid_mariadb_proxy_metadata", lambda: self.maria_restore(info))
        self.assertFalse(any(argv == ["server-start"] for argv, _ in self.calls))

    def test_maria_proxy_metadata_unknown_grantee_or_named_target_rejected(self):
        valid = {"user": "app_owner", "host": "%", "proxied_user": "", "proxied_host": "", "with_grant": True}
        for grant in ({**valid, "host": "localhost"}, {**valid, "proxied_user": "missing"}):
            info = maria_info()
            info["proxy_grants"] = [grant]
            self.error("unknown_mariadb_proxy_account", lambda: self.maria_restore(info))
        self.assertEqual(engines._maria_proxy_grants([valid], maria_info()["login_accounts"]), [valid])

    def test_maria_proxy_export_records_semantic_metadata_and_rechecks_changes(self):
        self.cfg.engine, self.cfg.databases = "mariadb", ["alpha", "beta"]
        grant = {"user": "app_owner", "host": "%", "proxied_user": "", "proxied_host": "", "with_grant": 1}
        def native(argv, **kwargs):
            result = self.fake_run(argv, **kwargs)
            if b"FROM mysql.proxies_priv" in kwargs.get("input_data", b""):
                return Result(0, json.dumps([grant]).encode())
            return result
        with patch.object(engines, "run", native):
            info = engines.export_database(self.cfg, self.stage)
        self.assertIs(info["proxy_grants"][0]["with_grant"], True)
        self.assertEqual(info["proxy_grants"][0]["proxied_host"], "")

    def test_maria_proxy_export_detects_changed_source_or_denied_metadata(self):
        self.cfg.engine, self.cfg.databases = "mariadb", ["alpha", "beta"]
        count = [0]
        def drift(argv, **kwargs):
            result = self.fake_run(argv, **kwargs)
            if b"FROM mysql.proxies_priv" in kwargs.get("input_data", b""):
                count[0] += 1
                grants = [] if count[0] == 1 else [{"user": "app_owner", "host": "%", "proxied_user": "", "proxied_host": "", "with_grant": 1}]
                return Result(0, json.dumps(grants).encode())
            return result
        with patch.object(engines, "run", drift):
            self.error("mariadb_proxy_changed_during_export", lambda: engines.export_database(self.cfg, self.stage))

    def test_maria_exact_proxy_restore_does_not_keep_seed_or_bootstrap_grants(self):
        info = maria_info()
        info["proxy_grants"] = [{"user": "app_owner", "host": "%", "proxied_user": "", "proxied_host": "", "with_grant": True}]
        synced = [False]
        def native(argv, **kwargs):
            result = self.fake_run(argv, **kwargs)
            sql = kwargs.get("input_data", b"")
            if b"DELETE FROM mysql.proxies_priv" in sql:
                synced[0] = True
            if b"FROM mysql.proxies_priv" in sql and b"JSON_ARRAYAGG" in sql:
                self.assertTrue(synced[0])
                return Result(0, json.dumps(info["proxy_grants"]).encode())
            return result
        self.maria_restore(info, native)
        sql = b"\n".join(k.get("input_data", b"") for _, k in self.calls)
        self.assertIn(b"INSERT INTO mysql.proxies_priv", sql)
        self.assertIn(b"DROP USER 'k3s_restore_", sql)
        self.assertNotIn(b"ALTER USER", sql)
        self.assertEqual("server-stopped", self.calls[-1][0][0])

    def test_maria_proxy_readback_mismatch_or_query_denied_is_fatal(self):
        info = maria_info()
        info["proxy_grants"] = [{"user": "app_owner", "host": "%", "proxied_user": "", "proxied_host": "", "with_grant": True}]
        self.error("mariadb_proxy_restore_mismatch", lambda: self.maria_restore(info))
        self.assertEqual("server-stopped", self.calls[-1][0][0])
        def denied(argv, **kwargs):
            if b"DELETE FROM mysql.proxies_priv" in kwargs.get("input_data", b""):
                raise BackupError("command_failed", "Synthetic PROXY permission denied")
            return self.fake_run(argv, **kwargs)
        self.error("command_failed", lambda: self.maria_restore(runner=denied))
        self.assertEqual("server-stopped", self.calls[-1][0][0])

    def test_maria_proxy_sync_clears_seed_grants_for_empty_source_set(self):
        self.maria_restore()
        sql = b"\n".join(k.get("input_data", b"") for _, k in self.calls)
        self.assertIn(b"DELETE FROM mysql.proxies_priv; FLUSH PRIVILEGES;", sql)
        self.assertNotIn(b"INSERT INTO mysql.proxies_priv", sql)
        self.assertLess(sql.index(b"DELETE FROM mysql.proxies_priv"), sql.rindex(b"DROP USER 'k3s_restore_"))

    def test_maria_extra_login_or_missing_system_view_account_fails_closed(self):
        def extra(argv, **kwargs):
            result = self.fake_run(argv, **kwargs)
            if b"SELECT IF(COUNT(*)=" in kwargs.get("input_data", b""):
                return Result(0, b"0\n")
            return result
        self.error("mariadb_account_restore_mismatch", lambda: self.maria_restore(runner=extra))
        self.assertEqual("server-stopped", self.calls[-1][0][0])
        def missing_sys(argv, **kwargs):
            result = self.fake_run(argv, **kwargs)
            if b"FROM mysql.user WHERE is_role='N'" in kwargs.get("input_data", b""):
                return Result(0, json.dumps([{"user": "mariadb.sys", "host": "localhost"}]).encode())
            return result
        self.error("mariadb_system_account_metadata_required", lambda: self.maria_restore(runner=missing_sys))
        self.assertEqual("server-stopped", self.calls[-1][0][0])

    def test_maria_restore_failure_stops_and_keeps_input(self):
        self.cfg.engine = "mariadb"
        self.cfg.databases = ["alpha", "beta"]
        info = maria_info()
        self.payload(info)
        def broken(argv, **kw):
            if kw.get("input_path"):
                raise BackupError("command_failed", "Synthetic restore failed")
            return self.fake_run(argv, **kw)
        with patch.object(engines, "run", broken), patch.object(engines, "_database_access", self.access), patch.object(engines, "_maria_server", self.server):
            self.error("command_failed", lambda: engines.restore_database(self.cfg, self.stage, self.target, {"engine": info}))
        self.assertTrue((self.stage / "databases.sql").exists())
        self.assertEqual("server-stopped", self.calls[-1][0][0])

    def test_legacy_physical_risk_exact_major_clean_shutdown(self):
        (self.target / "PG_VERSION").write_text("17\n")
        self.error("legacy_physical_risk_required", lambda: engines.validate_physical(self.cfg, self.target, None))
        self.cfg.env["ALLOW_LEGACY_PHYSICAL"] = "true"
        self.cfg.target_major = "16"
        self.error("physical_major_mismatch", lambda: engines.validate_physical(self.cfg, self.target, None))
        self.cfg.target_major = "17"
        with patch.object(engines, "run", self.fake_run), warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            engines.validate_physical(self.cfg, self.target, None)
        self.assertIn("legacy_physical_consistency_unknown", str(caught[0].message))
        with patch.object(engines, "run", return_value=Result(0, b"Database cluster state: in production\n")):
            self.error("physical_recovery_required", lambda: engines.validate_physical(self.cfg, self.target, None))

    def test_physical_maria_not_fake_implemented(self):
        self.cfg.engine = "mariadb"
        self.error("physical_not_implemented", lambda: engines.export_physical(self.cfg, self.stage))
        self.error("physical_not_implemented", lambda: engines.validate_physical(self.cfg, self.target, None))

    def test_native_basebackup_verifies_not_live_copy(self):
        def physical(argv, **kw):
            if Path(argv[0]).name == "pg_basebackup":
                p = Path(next(a.split("=", 1)[1] for a in argv if a.startswith("--pgdata=")))
                p.mkdir()
                (p / "PG_VERSION").write_text("17\n")
            return self.fake_run(argv, **kw)
        with patch.object(engines, "run", physical):
            info = engines.export_physical(self.cfg, self.stage)
        self.assertEqual("pg-physical", info["kind"])
        base = next(a for a, _ in self.calls if Path(a[0]).name == "pg_basebackup")
        self.assertIn("--wal-method=stream", base)
        self.assertTrue(any(Path(a[0]).name == "pg_verifybackup" for a, _ in self.calls))


class LegacyAndLifecycleTests(unittest.TestCase):
    setUp = EnginesTests.setUp
    tearDown = EnginesTests.tearDown
    fake_run = EnginesTests.fake_run
    error = EnginesTests.error
    access = EnginesTests.access
    server = EnginesTests.server

    def legacy_setup(self):
        import hashlib
        archive = self.stage / "generic.pgdump"
        archive.write_bytes(b"PGDMPsynthetic-custom-only")
        globals_file = self.stage / "globals-no-passwords.sql"
        globals_file.write_text("CREATE ROLE app_owner;\nALTER ROLE app_owner WITH LOGIN;\n")
        sums = "".join(hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.name + "\n" for p in (archive, globals_file))
        (self.stage / "SHA256SUMS").write_text(sums)
        metadata = self.root / "metadata.json"
        metadata.write_text(json.dumps([pg_db()]))
        metadata.chmod(0o600)
        self.cfg.env["LEGACY_DATABASE_METADATA_FILE"] = str(metadata)
        return archive, globals_file

    def test_legacy_checksum_header_decode_and_metadata(self):
        self.legacy_setup()
        with patch.object(engines, "run", self.fake_run):
            info = engines.legacy_manifest(self.cfg, self.stage)
        self.assertTrue(info["legacy_native"])
        self.assertEqual("generic.pgdump", info["databases"][0]["archive"])
        self.assertEqual(["app_owner"], info["login_roles"])
        self.assertIn("operator-declared", info["metadata_evidence"])
        self.assertTrue(any("--file=/dev/null" in a for a, _ in self.calls))

    def test_legacy_rejects_checksum_corruption_before_native_tools(self):
        archive, _ = self.legacy_setup()
        archive.write_bytes(b"PGDMPsynthetic-corrupted")
        with patch.object(engines, "run", side_effect=AssertionError("no native execution")):
            self.error("legacy_checksum_mismatch", lambda: engines.legacy_manifest(self.cfg, self.stage))

    def test_legacy_rejects_traversal_checksum_entry(self):
        self.legacy_setup()
        (self.stage / "SHA256SUMS").write_text("0" * 64 + "  ../outside\n")
        self.error("invalid_database_archive", lambda: engines.legacy_manifest(self.cfg, self.stage))

    def test_legacy_does_not_guess_extension_versions_or_identity(self):
        self.error("legacy_metadata_required", lambda: engines.legacy_manifest(self.cfg, self.stage))
        self.legacy_setup()
        def wrong_db(argv, **kw):
            if "--list" in argv:
                return Result(0, b"; dbname: unrelated\n; Dumped from database version: 17.11\n; Dumped by pg_dump version: 17.11\n")
            return self.fake_run(argv, **kw)
        with patch.object(engines, "run", wrong_db):
            self.error("legacy_header_invalid", lambda: engines.legacy_manifest(self.cfg, self.stage))

    def test_old_extension_install_sql_available_without_default_upgrade(self):
        d = pg_db()
        d["extensions"][1]["version"] = "0.8.2"
        (self.share / "vector--0.8.2.sql").write_text("-- synthetic install only\n")
        with patch.object(engines, "run", self.fake_run):
            engines._check_pg_requirements(self.cfg, "17", [d])
        self.assertEqual("0.8.2", d["extensions"][1]["version"])

    def test_native_physical_recovery_validates_and_shuts_down(self):
        (self.target / "PG_VERSION").write_text("17\n")
        (self.target / "pg_hba.conf").write_text("local all all trust\n")
        (self.target / "postgresql.conf").write_text("# synthetic\n")
        info = pg_info()
        info.update(kind="pg-physical", physical_native=True)
        with patch.object(engines, "run", self.fake_run), patch.object(engines, "_database_access", self.access), patch.object(engines, "_pg_server", self.server):
            engines.validate_physical(self.cfg, self.target, {"engine": info})
        self.assertTrue(any(Path(a[0]).name == "pg_verifybackup" for a, _ in self.calls))
        self.assertTrue(any(b"pg_is_in_recovery()" in k.get("input_data", b"") for _, k in self.calls))
        self.assertTrue(any(a == ["server-stopped"] for a, _ in self.calls))

    def test_scram_passwords_follow_postgres_saslprep(self):
        with patch.object(engines.secrets, "token_bytes", return_value=b"x" * 16):
            self.assertEqual(engines._scram("I\u00ADX"), engines._scram("IX"))
            self.assertEqual(engines._scram("a\u00a0b"), engines._scram("a b"))
            self.assertEqual(engines._scram("\u2168"), engines._scram("IX"))

    def test_extension_install_version_cannot_traverse_share_directory(self):
        d = pg_db()
        d["extensions"][1]["version"] = "../../outside"
        with patch.object(engines, "run", self.fake_run):
            self.error("unsupported_extension", lambda: engines._check_pg_requirements(self.cfg, "17", [d]))

    def test_maria_install_limits_redo_and_removes_passwordless_seed_users(self):
        self.cfg.engine = "mariadb"
        self.cfg.databases = ["alpha", "beta"]
        info = maria_info()
        info["login_accounts"].append({"user": "mariadb.sys", "host": "localhost", "global_grant_option": False})
        EnginesTests.payload(self, info)
        def with_seed(argv, **kw):
            value = self.fake_run(argv, **kw)
            if (b"JSON_ARRAYAGG" in kw.get("input_data", b"") and b"FROM mysql.user" in kw.get("input_data", b"")
                    and b"global_grant_option" not in kw.get("input_data", b"")):
                return Result(0, json.dumps([{"user": "root", "host": "localhost"}, {"user": "", "host": "localhost"},
                                            {"user": "mariadb.sys", "host": "localhost"}, {"user": "mysql", "host": "localhost"},
                                            {"user": "postgres", "host": "localhost"}]).encode())
            return value
        with patch.object(engines, "run", with_seed), patch.object(engines, "_database_access", self.access), patch.object(engines, "_maria_server", self.server):
            engines.restore_database(self.cfg, self.stage, self.target, {"engine": info})
        install = next(a for a, _ in self.calls if any(Path(x).name == "mariadb-install-db" for x in a))
        self.assertIn("--innodb-log-file-size=8M", install)
        sql = b"\n".join(k.get("input_data", b"") for _, k in self.calls)
        self.assertIn(b"DROP USER IF EXISTS 'root'@'localhost'", sql)
        self.assertIn(b"DROP USER IF EXISTS ''@'localhost'", sql)
        self.assertNotIn(b"DROP USER IF EXISTS 'mariadb.sys'", sql)
        self.assertIn(b"DROP USER IF EXISTS 'mysql'@'localhost'", sql)
        self.assertIn(b"DROP USER IF EXISTS 'postgres'@'localhost'", sql)
        import_index = next(i for i, (_, k) in enumerate(self.calls) if k.get("input_path"))
        self.assertFalse(any(b"DROP USER" in k.get("input_data", b"") for _, k in self.calls[:import_index]))

    def test_maria_bootstrap_can_restore_native_proxy_grants_and_is_removed(self):
        self.cfg.engine = "mariadb"
        self.cfg.databases = ["alpha", "beta"]
        info = maria_info()
        EnginesTests.payload(self, info)
        with patch.object(engines, "run", self.fake_run), patch.object(engines, "_database_access", self.access), patch.object(engines, "_maria_server", self.server):
            engines.restore_database(self.cfg, self.stage, self.target, {"engine": info})
        create = next(k["input_data"] for _, k in self.calls if b"CREATE USER" in k.get("input_data", b""))
        import re
        proxy = re.search(rb"GRANT PROXY ON ''@'%' TO ('k3s_restore_[0-9a-f]+'@'localhost') WITH GRANT OPTION;", create)
        self.assertIsNotNone(proxy)
        account = proxy.group(1)
        last_sql = next(k["input_data"] for _, k in reversed(self.calls) if k.get("input_data"))
        self.assertIn(b"DROP USER " + account + b";", last_sql)
        self.assertNotIn(self.cfg.db_password.encode(), create)
        self.assertTrue(any(k.get("input_path") == self.stage / "globals.sql" for _, k in self.calls))

    def test_native_toc_create_excludes_data_and_installs_requested_version(self):
        with patch.object(engines, "run", self.fake_run):
            db = pg_db()
            db["extensions"][1]["version"] = "0.8.2"
            engines._create_pg_database(self.cfg, "17", self.stage / "db.dump", db,
                                        engines._pg_conn(self.cfg, "template1"), self.root)
        sql = b"\n".join(k.get("input_data", b"") for _, k in self.calls)
        self.assertIn(b"VERSION '0.8.2'", sql)
        self.assertNotIn(b"VERSION '0.8.7'", sql)
        creates = [a for a, _ in self.calls if Path(a[0]).name == "pg_restore" and "--create" in a and "--list" not in a]
        self.assertEqual(1, len(creates))
        toc = next(a for a, _ in self.calls if Path(a[0]).name == "pg_restore" and "--list" in a)
        self.assertIn("--create", toc)
        self.assertTrue(any(a.startswith("--use-list=") for a in creates[0]))
        restores = [a for a, _ in self.calls if Path(a[0]).name == "pg_restore" and "--dbname=dbname='alpha'" in a]
        self.assertTrue(restores)
        self.assertNotIn("--create", restores[0])

    def test_signals_drop_to_same_uid_without_cap_kill(self):
        from unittest.mock import Mock
        proc = Mock(pid=12345)
        proc.poll.return_value = None
        def signal_run(argv, **kw):
            self.assertFalse(kw["check"])
            self.assertIn("--reuid=123", argv)
            self.assertIn("--regid=456", argv)
            self.assertIn("-12345", argv)
            self.assertNotIn(self.cfg.db_password, " ".join(argv))
            return Result(0)
        with patch.object(engines, "run", signal_run), patch.object(engines.os, "geteuid", return_value=0):
            engines._signal_database(self.cfg, proc, 15, (123, 456), group=True)

    def test_pg_server_failure_kills_group_and_uses_parent_death_signal(self):
        from unittest.mock import Mock
        proc = Mock(pid=12345)
        proc.poll.return_value = None
        with patch.object(engines.subprocess, "Popen", return_value=proc) as popen, patch.object(engines, "run", return_value=Result(0)), patch.object(engines, "_kill_group") as kill, patch.object(engines.Path, "exists", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                with engines._pg_server(self.cfg, "17", self.target, self.root, (123, 456)):
                    raise RuntimeError("synthetic")
        kill.assert_called_once_with(self.cfg, proc, (123, 456))
        self.assertIn("--pdeathsig=TERM", popen.call_args[0][0])
        self.assertNotIn("shell", popen.call_args.kwargs)


class PostgresOwnerHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-owner-handoff-")
        self.root = Path(self.tmp.name)
        self.target = self.root / "owned-output"
        self.target.mkdir(mode=0o700)
        self.child = self.target / "base"
        self.child.mkdir(mode=0o700)
        self.file = self.child / "page"
        self.file.write_bytes(b"synthetic-only")
        self.file.chmod(0o600)
        self.cfg = SimpleNamespace(env={})
        self.helper = (os.geteuid(), os.getegid())
        self.expected = (self.target.stat().st_dev, self.target.stat().st_ino)

    def tearDown(self):
        self.tmp.cleanup()

    def error(self, code, callback):
        with self.assertRaises(BackupError) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)

    def transfer(self, final=(70, 70), clean=True):
        return engines._handoff_database_owner(self.target, self.helper, final, self.expected, clean_shutdown=clean)

    def identities(self, env=None, *, root=True, account=None):
        self.cfg.env = env or {}
        account = account or SimpleNamespace(pw_name="postgres", pw_uid=999, pw_gid=999)
        with patch.object(engines.os, "geteuid", return_value=0 if root else 1234), patch.object(engines.os, "getegid", return_value=1234), patch.object(engines.pwd, "getpwnam", return_value=account), patch.object(engines.pwd, "getpwuid", return_value=account):
            return engines._pg_restore_identities(self.cfg)

    def test_helper_has_nss_account_final_numeric_uid_does_not_need_one(self):
        self.assertEqual(((999, 999), (70, 70)), self.identities({"DB_UID": "70", "DB_GID": "70"}))
        self.assertEqual(((999, 999), (999, 999)), self.identities())
        self.assertEqual(((999, 999), (70, 71)), self.identities({"POSTGRES_UID": "70", "POSTGRES_GID": "71"}))
        self.assertEqual(((999, 999), (72, 73)), self.identities({"DB_UID": "72", "DB_GID": "73", "POSTGRES_UID": "70", "POSTGRES_GID": "71"}))

    def test_missing_helper_nss_or_root_helper_fails_closed(self):
        with patch.object(engines.os, "geteuid", return_value=0), patch.object(engines.pwd, "getpwnam", side_effect=KeyError("synthetic")):
            self.error("database_identity", lambda: engines._pg_restore_identities(self.cfg))
        self.error("database_identity", lambda: self.identities(account=SimpleNamespace(pw_name="root", pw_uid=0, pw_gid=0)))

    def test_final_ids_reject_zero_negative_noninteger_and_uid_sentinel(self):
        for value in ("0", "-1", "word", "1.5", "", "4294967295", "4294967296", "9" * 100):
            with self.subTest(value=value):
                self.error("database_identity", lambda: self.identities({"DB_UID": value, "DB_GID": "70"}))
        self.error("database_identity", lambda: self.identities({"DB_UID": "70", "DB_GID": "0"}))

    def test_nonroot_only_current_identity_with_resolvable_nss(self):
        self.assertEqual(((1234, 1234), (1234, 1234)), self.identities(root=False))
        self.error("database_identity", lambda: self.identities({"DB_UID": "70", "DB_GID": "70"}, root=False))
        with patch.object(engines.os, "geteuid", return_value=1234), patch.object(engines.pwd, "getpwuid", side_effect=KeyError("synthetic")):
            self.error("database_identity", lambda: engines._pg_restore_identities(self.cfg))

    def test_active_server_or_no_clean_shutdown_never_changes_owner(self):
        with patch.object(engines.os, "fchown") as change:
            self.error("database_handoff_active", lambda: self.transfer(clean=False))
            for marker in ("postmaster.pid", "server.pid"):
                p = self.target / marker
                p.write_text("synthetic-active-marker")
                with patch.object(engines.os, "geteuid", return_value=0):
                    self.error("database_handoff_active", lambda: self.transfer())
                p.unlink()
            change.assert_not_called()

    def test_same_final_identity_has_no_handoff_or_permission_changes(self):
        with patch.object(engines.os, "fchown") as change, patch.object(engines, "_handoff_open_target") as opening:
            self.transfer(self.helper)
        change.assert_not_called()
        opening.assert_not_called()

    def test_nonroot_handoff_to_different_owner_is_rejected(self):
        with patch.object(engines.os, "geteuid", return_value=1234):
            self.error("database_identity", lambda: self.transfer())

    def test_preflight_rejects_symlink_and_hardlink_before_any_chown(self):
        outside = self.root / "unrelated"
        outside.write_bytes(b"must-preserve")
        for kind in ("symlink", "hardlink"):
            path = self.target / kind
            if kind == "symlink":
                path.symlink_to(outside)
            else:
                os.link(outside, path)
            with patch.object(engines.os, "geteuid", return_value=0), patch.object(engines.os, "fchown") as change:
                self.error("unsafe_database_tree", lambda: self.transfer())
                change.assert_not_called()
            path.unlink()
        self.assertEqual(b"must-preserve", outside.read_bytes())

    def test_preflight_rejects_special_file_and_changed_root(self):
        fifo = self.target / "fifo"
        os.mkfifo(fifo)
        with patch.object(engines.os, "geteuid", return_value=0), patch.object(engines.os, "fchown") as change:
            self.error("unsafe_database_tree", lambda: self.transfer())
            change.assert_not_called()
        fifo.unlink()
        self.expected = (self.expected[0], self.expected[1] + 1)
        with patch.object(engines.os, "geteuid", return_value=0), patch.object(engines.os, "fchown") as change:
            self.error("database_handoff_identity", lambda: self.transfer())
            change.assert_not_called()

    def test_preflight_rejects_mount_boundary_before_any_chown(self):
        inode = self.file.stat().st_ino
        native_mount = engines._handoff_mount_id
        def changed_mount(fd):
            return native_mount(fd) + (1 if os.fstat(fd).st_ino == inode else 0)
        with patch.object(engines.os, "geteuid", return_value=0), patch.object(engines, "_handoff_mount_id", side_effect=changed_mount), patch.object(engines.os, "fchown") as change:
            self.error("database_handoff_mount", lambda: self.transfer())
            change.assert_not_called()

    def test_foreign_file_owner_is_rejected_before_any_chown(self):
        with patch.object(engines.os, "geteuid", return_value=0), patch.object(engines.os, "fchown") as change:
            self.error("database_handoff_owner", lambda: engines._handoff_database_owner(self.target, (999, 999), (70, 70), self.expected, clean_shutdown=True))
            change.assert_not_called()

    def test_pg_logical_flow_uses_helper_and_handoffs_only_after_clean_stop(self):
        case = EnginesTests(methodName="test_pg_restore_failure_always_stops_server")
        case.setUp()
        try:
            info = pg_info()
            case.payload(info)
            def do_handoff(target, helper, final, expected, *, clean_shutdown):
                self.assertEqual((999, 999), helper)
                self.assertEqual((70, 70), final)
                self.assertTrue(clean_shutdown)
                self.assertEqual((target.stat().st_dev, target.stat().st_ino), expected)
                self.assertIsInstance(expected, tuple)
                self.assertTrue(any(a == ["server-stopped"] for a, _ in case.calls))
                self.assertTrue(any(Path(a[0]).name == "pg_controldata" for a, _ in case.calls))
            with patch.object(engines, "_pg_restore_identities", return_value=((999, 999), (70, 70))), patch.object(engines, "_database_access", case.access), patch.object(engines, "_pg_server", case.server), patch.object(engines, "run", case.fake_run), patch.object(engines, "_handoff_database_owner", side_effect=do_handoff) as handoff:
                engines.restore_database(case.cfg, case.stage, case.target, {"engine": info})
            handoff.assert_called_once()
            init = next(a for a, _ in case.calls if any(Path(x).name == "initdb" for x in a))
            if os.geteuid() == 0:
                self.assertIn("--reuid=999", init)
                self.assertNotIn("--reuid=70", init)
        finally:
            case.tearDown()

    def test_failed_pg_logical_restore_or_unclean_control_does_not_handoff(self):
        for failure in ("restore", "shutdown"):
            with self.subTest(failure=failure):
                case = EnginesTests(methodName="test_pg_restore_failure_always_stops_server")
                case.setUp()
                try:
                    info = pg_info()
                    case.payload(info)
                    def failed(argv, **kw):
                        if failure == "restore" and Path(argv[0]).name == "pg_restore":
                            raise BackupError("command_failed", "Synthetic restore failure")
                        if failure == "shutdown" and Path(argv[0]).name == "pg_controldata":
                            return Result(0, b"Database cluster state: in production\n")
                        return case.fake_run(argv, **kw)
                    with patch.object(engines, "_pg_restore_identities", return_value=((999, 999), (70, 70))), patch.object(engines, "_database_access", case.access), patch.object(engines, "_pg_server", case.server), patch.object(engines, "run", failed), patch.object(engines, "_handoff_database_owner") as handoff:
                        with self.assertRaises(BackupError):
                            engines.restore_database(case.cfg, case.stage, case.target, {"engine": info})
                    handoff.assert_not_called()
                finally:
                    case.tearDown()

    def test_final_tree_handoff_is_pinned_mode_preserving_and_outside_untouched(self):
        # Emulate fchown ownership bookkeeping so this test works as an ordinary
        # CI runner as well as root, without requiring privileged chown/caps.
        native_stat = os.fstat
        native_chmod = os.fchmod
        owners, changes = {}, []
        before_parent = self.root.stat()
        def fstat(fd):
            info = native_stat(fd)
            if info.st_ino not in owners:
                return info
            values = list(info)
            values[4], values[5] = owners[info.st_ino]
            return os.stat_result(values)
        def fchown(fd, uid, gid):
            info = native_stat(fd)
            changes.append((info.st_ino, uid, gid))
            owners[info.st_ino] = (uid, gid)
        with patch.object(engines.os, "geteuid", return_value=0), patch.object(engines.os, "fstat", side_effect=fstat), patch.object(engines.os, "fchown", side_effect=fchown), patch.object(engines.os, "fchmod", side_effect=native_chmod):
            self.transfer()
        self.assertEqual({self.target.stat().st_ino, self.child.stat().st_ino, self.file.stat().st_ino}, {i for i, _, _ in changes})
        self.assertEqual(self.target.stat().st_ino, changes[-1][0])
        self.assertTrue(all((uid, gid) == (70, 70) for _, uid, gid in changes))
        self.assertEqual(0o600, stat.S_IMODE(self.file.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(self.target.stat().st_mode))
        self.assertEqual((before_parent.st_uid, before_parent.st_gid, before_parent.st_mode), (self.root.stat().st_uid, self.root.stat().st_gid, self.root.stat().st_mode))
        self.assertEqual(b"synthetic-only", self.file.read_bytes())

    def test_chown_or_fsync_failure_never_reports_success(self):
        for method in ("fchown", "fsync"):
            with self.subTest(method=method), patch.object(engines.os, "geteuid", return_value=0), patch.object(engines.os, method, side_effect=OSError("synthetic")):
                self.error("database_handoff_failed", lambda: self.transfer())


if __name__ == "__main__":
    unittest.main()
