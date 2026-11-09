#!/bin/sh
# Tool/profile inventory only; native container E2E is required separately.
set -eu
python3 - <<'PY'
import ctypes
import importlib.util
import json
import locale
import pathlib
import pwd
import re
import shutil
import socket
import ssl
import subprocess
import sys
import zoneinfo

assert sys.version_info[:2] == (3, 11), "python_version_mismatch"
assert importlib.util.find_spec("k3s_backup"), "package_missing"
assert pathlib.Path("/etc/ssl/certs/ca-certificates.crt").stat().st_size > 0
assert ssl.create_default_context().get_ca_certs(), "ca_bundle_not_loadable"
assert socket.getaddrinfo("localhost", 443), "nss_resolution_missing"
assert locale.setlocale(locale.LC_ALL, "en_US.UTF-8"), "locale_runtime_missing"
assert zoneinfo.ZoneInfo("Asia/Shanghai"), "timezone_runtime_missing"
assert ctypes.CDLL(None).getpid() > 0, "ctypes_runtime_missing"

p = pathlib.Path("/usr/share/k3s-restic/profile.json")
profile = json.loads(p.read_text()) if p.is_file() else {
    "schema_version": 1, "profile": "build-all", "files": True,
    "postgres_majors": ["16", "17"], "mariadb_series": ["11.8"]}
assert profile.get("schema_version") == 1 and profile.get("files") is True, "invalid_image_profile"
pg = [str(x) for x in profile.get("postgres_majors", [])]
maria = [str(x) for x in profile.get("mariadb_series", [])]
assert set(pg) <= {"16", "17"} and set(maria) <= {"11.8"}, "unsupported_profile_engine"

for binary in ("restic", "rsync", "cron", "flock", "runuser", "setpriv", "locale"):
    assert shutil.which(binary), "required_tool_missing:" + binary

def version(argv):
    return subprocess.check_output(argv, stderr=subprocess.STDOUT, timeout=30).decode()

assert re.search(r"^restic 0\.19\.1(?: |$)", version(["restic", "version"])), "restic_version_mismatch"
for major in pg:
    assert pwd.getpwnam("postgres").pw_uid == 999, "database_uid_mismatch"
    exact = {"16": "16.15", "17": "17.11"}[major]
    root = pathlib.Path("/usr/lib/postgresql") / major / "bin"
    for tool in ("postgres", "initdb", "pg_ctl", "psql", "pg_dump", "pg_dumpall", "pg_restore",
                 "pg_controldata", "pg_basebackup", "pg_verifybackup", "pg_isready"):
        assert (root / tool).is_file(), "postgres_tool_missing:" + tool
        assert exact in version([str(root / tool), "--version"]), "postgres_version_mismatch"
    control = pathlib.Path("/usr/share/postgresql") / major / "extension/vector.control"
    assert "0.8.7" in control.read_text(), "pgvector_version_mismatch"
    for name in ("vector--0.8.2.sql", "vector--0.8.2--0.8.3.sql"):
        assert (control.parent / name).is_file(), "pgvector_historical_sql_missing"
    assert (pathlib.Path("/usr/lib/postgresql") / major / "lib/vector.so").is_file(), "pgvector_library_missing"
for major in {"16", "17"} - set(pg):
    assert not pathlib.Path("/usr/lib/postgresql", major).exists(), "unrelated_postgres_in_profile"
    assert not pathlib.Path("/usr/share/postgresql", major).exists(), "unrelated_postgres_share_in_profile"

maria_tools = ("mariadb", "mariadb-dump", "mariadb-install-db", "mariadb-admin", "mariadbd", "mariadb-backup")
if maria:
    assert pwd.getpwnam("mysql").pw_uid == 998, "database_uid_mismatch"
    for binary in maria_tools:
        assert shutil.which(binary), "required_tool_missing:" + binary
    assert "11.8.9" in version(["mariadbd", "--version"]), "mariadb_version_mismatch"
else:
    for binary in maria_tools:
        assert not shutil.which(binary), "unrelated_mariadb_in_profile"
    assert not pathlib.Path("/usr/share/mariadb").exists(), "unrelated_mariadb_share_in_profile"
locales = version(["locale", "-a"]).lower().splitlines()
assert "en_us.utf8" in locales and "c.utf8" in locales, "utf8_locale_missing"
print("PASS image profile inventory: " + profile["profile"] + " (not container/database E2E)")
PY
