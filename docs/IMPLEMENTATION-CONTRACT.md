# Local implementation contract (2026-11-09)

One package under k3s_backup/, Python3.11 stdlib at runtime. No production interactions, no credentials, no real snapshots in tests. Source root is current public clone, branch feature/k3s-unified-backup.

## Core owned by Lead

config.py exposes Config.from_env(env=None), config.engine (files/postgres/mariadb), config.dataset_id, config.data_path (Path), config.work_path (Path), config.databases (list[str]), config.db_host/port/user/password, config.target_major (str|None), config.env (sanitized only when logging; full private runtime mapping), config.run_timeout. Absolute binary paths can be env-configured for tests. Environment compatibility DATABASE_TYPE aliases, DATABASE_*/PG_*/MYSQL_*/MARIADB_*, *_FILE. Independent-repo mode explicit true default (legacy one repo per project); dataset defaults repo-derived digest stable across Pod/node/repository credential changes; shared repo requires DATASET_ID. No UID-derived identity.

process.py: run(argv: list[str], *, env: dict|None=None, cwd: Path|None=None, input_data: bytes|None=None, timeout: int=900, stdout_path: Path|None=None) -> Result(returncode:int, stdout:bytes, stderr:bytes). Must not print raw args/env/error stderr. Nonzero raises errors.BackupError(code, safe message) ONLY when helper check=True; otherwise caller validates rc. Signature will default check=True and accept check=False. Bound child process group on deadline; no shell=True. Engine adapters call run(check=True) and decode only allowlisted metadata. Password via env or mode0600 temp option file, never argv.

errors.py: BackupError(code: str, message: str), message safe for user output.

## Engine teammate write scope

k3s_backup/engines.py + tests/test_engines.py (+ tests/fixtures/engine*.sql if desired). Implement Engine API:

- export_database(config, stage:Path) -> dict metadata. Must export native archives under stage, all declared databases and globals; metadata includes engine,target_major,databases list, locale/extensions and archive names. No manifest write (Lead does this). Nonzero errors propagate. Output artifacts only relative names, no secrets.
- restore_database(config, stage:Path, target:Path, manifest:dict) -> None. Restores logical archives already verified by Lead into EMPTY temporary target, starts short-lived local PG16/17 or MariaDB server from unified image, restores globals/roles/DBs (no password plaintext artifacts), injects config password via safe connection input, closes server in finally, fsync/clean shutdown on success; no TCP listener needed, no main DB Service. Target published by restore module after success. Unsupported version/extension/locale explicit BackupError, no fake success. No pg_upgrade/raw physical claim. Must run root->dedicated DB uid or current uid, configure final host auth, preserve uid/gid configured in env. PG unlogged servers only Unix socket, trust local temporary socket dirs0700 allowed; final hba scram.
- validate_physical(config, target:Path, manifest:dict|None) -> None. Explicit legacy physical compatibility major check, pg_controldata + recovery allowed only with legacy risk option; do not assert live file copy consistent. File mode has no engine call.

Manifest schema used for new snapshots:
{schema_version:1,dataset_id:str,kind:files|pg-logical|mariadb-logical,created_at:UTC ISO,engine:dict returned by adapter,artifacts:[{path:relative str,sha256:hex,bytes:int,type:file|symlink,link_target?:str}],source_path:str,complete:true}. Sensitive DB names may be present encrypted in restic manifest, never logs. New native stage also holds payload subdir or db files; reserved manifest filename backup-manifest.json.

## Restore teammate write scope

k3s_backup/restore.py + tests/test_restore.py. Only safe volume ownership/publication, no repository or engine deps. API:
restore_to_volume(config, snapshot_id:str, populate:Callable[[Path],None]) -> dict. Uses config.data_path target, config.work_path scratch parent, stable config.dataset_id. Skip unrelated nonempty target by default (FORCESYNC true maps explicit policy but never delete unrelated data); own .k3s-restic-restore.json marker records dataset/snapshot/phase and owned entries, robust against interrupted copying. Staging must be on destination filesystem for atomic per-entry rename; mount-root target cannot be renamed. Retry only owned marker/stage; untrusted marker/symlink/path must reject. No global rm -rf, marker metadata not mistaken for app data. fsync and complete marker as appropriate; final success only after populate+publish. Returns restored/skipped status safe. May add helper require_empty/dir validation. Do not import engines/core (Lead calls callback).

## Image/CI teammate write scope

Dockerfile, .dockerignore, .github/workflows/*, tests/container_e2e.py, docs/IMAGE.md, scripts/image-selftest.sh. One Debian multiarch final image containing coexisting PG16/17 server+client, pgvector for both, native MariaDB server/client, verified restic0.19.1 + CA/Python/rsync/cron utilities. Fixed base digest/package versions/checksums; if package pin cannot resolve, report rather than invent. Entrypoint python3 -m k3s_backup; PYTHONPATH=/opt/k3s-restic. Build context ONLY repo, ignore .git/env/private/dumps. Package dirs controlled by lead; do not write core files. CI PR unit/file integration + DB container E2E; release tested tags to GHCR using GITHUB_TOKEN, pinned actions, amd64/arm64, SBOM/provenance + signatures. No publishing in local run. E2E flags must report NOT RUN/fail, not fake pass. Old .github/workflows/docker-publish.yml must not publish untested overlapping workflow: adjust/delete only after reading.

Lead owns docs/README/testing baseline outside assigned docs. All tasks disjoint; communicate contract assumptions before write. Final lead runs all tests/reviews.
