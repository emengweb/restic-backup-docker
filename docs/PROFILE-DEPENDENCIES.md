# All-in-one and lite runtime dependencies

## Final decision and scope

As of 2026-11-09 12:16 Asia/Shanghai, the user requests only two public image variants: **default all-in-one** and optional **lite**. Database-major/engine-specific image profiles and public tags are removed. Internal files is retained solely as the common base for lite/all; it is not a third public product. The all variant still contains PostgreSQL16 and17 plus MariaDB11.8 server/client/restore tooling. No database capability or native binary is removed from all by this cleanup.

Task48 permits changes only to scripts/collect-runtime.py, tests/test_runtime_closure.py and this document. Lead owns Dockerfile, core capability gates, selftest/E2E, builds and final acceptance. No Docker operation, image build, production access, private data or push is permitted in task48. Baseline filesystem free54,062,596,096 bytes; expected source edits<100KiB, no backup/data source. Rollback/review owner: Lead. Stop on an unexpected selection change or missing dependency.

## Collector interface

```sh
# Shared pure-files base / lite payload
python3 scripts/collect-runtime.py --profile files --output /opt/files-rootfs
# Optional direct lite equivalent
python3 scripts/collect-runtime.py --profile lite --output /opt/lite-rootfs
# Complete all-in-one payload
python3 scripts/collect-runtime.py --profile all --output /opt/all-rootfs
# All-only increment layered on the unchanged files base
python3 scripts/collect-runtime.py --profile all --base-profile files --output /opt/all-delta
```

Omitting --profile continues to select all. Only files, lite and all are accepted; obsolete single-engine/version choices fail argument validation. --base-profile accepts only files and is valid only with all. Files/lite cannot be subtracted from themselves. Output must be new/empty; source and base are never erased or edited.

## Capability and dependency contract

Every output writes /usr/share/k3s-restic/profile.json with schema_version1, profile, postgres_majors(list of strings), mariadb_series(list of strings) and files=true. All advertises PostgreSQL16+17 and MariaDB11.8. Files/lite advertises no database major/series. The public lite Docker target may retain the internal files marker because the capabilities and payload are identical. Lead owns profile-aware runtime rejection and selftest/E2E.

Files/lite retains the shared runner, restic, Python stdlib/lib-dynload, CA/NSS/PAM, locales/timezones, shell/archive/rsync and privilege/cron tools. It does not include database executables, server/client libraries, database plugins or initialization SQL. The MariaDB-specific optional pam_user_map.so and user_map.conf are excluded from files/lite while standard runuser PAM remains. All retains both PG majors, non-JIT shared objects/encoding modules/initdb catalogs/extensions, complete MariaDB share/plugins, native server/client/dump/admin/install/backup and all original tool lists. Existing LLVM/JIT and Galera exclusions are unchanged, not introduced by this two-variant cleanup.

All/files/lite source selections, recursive resource flags, runtime empty directories and capability JSON were compared before and after task48 using recording collectors and were exactly equal. The native TOOLS and PG_TOOLS tuples were also exactly equal. This establishes that removing obsolete profile selectors did not silently change either remaining dependency set; it does not replace a final native image E2E.

## Shared base and exact delta guarantees

All delta compares each full all-profile node to files by type, mode, UID/GID, file length+SHA256 or symlink target. Only changed/new payload files are emitted, plus canonical directory ancestors. Identical shared files and inherited usrmerge aliases are not recopied. There are no whiteouts, source deletions, base rewrites or hidden package-manager fallbacks. Identical empty runtime directories are inherited.

Delta runtime-files.json describes the effective full all profile and records base_profile=files; the small generated profile/inventory markers intentionally override the files-base metadata and exclude recursive self-hashes. Real Docker layer-prefix sharing must be confirmed by Lead; file-set equality alone is not registry-layer evidence.

## Historical measurements and current verification

The prior task45 metadata-only measurement used an approved private patched-runtime container with read-only filesystem/source, network none and only the specifically approved DAC_OVERRIDE capability needed to read mysql-owned plugin metadata. It started no database and finished exit0. These historical selected-file estimates remain relevant to the unchanged two payloads, not a new task48 image-size promise:

| Payload | Selected regular bytes | New payload over files |
| --- | ---: | ---: |
| files / lite | 101,266,689 | — |
| default all | 284,683,608 | 183,416,919 |

All shares3,206 exact common nodes with files in that historical measurement. Generated inventory overhead/compression and later source changes are not included. Old single-database estimates are no longer supported image offerings and are intentionally not listed.

### Task48 actual tests

- PASS:34 collector unit tests, including source-only closure safety, exact delta/hash/mode/UID/GID/link comparison, inherited symlink handling, base immutability, all capability map, lite/database isolation, removed profile rejection and all retaining both PG majors plus MariaDB.
- PASS:before/after recording comparison for all/files/lite showed identical selected paths, recursive/glob flags, empty directory metadata and capability JSON; native binary tuples unchanged. Recorded selections:all174, files105, lite105.
- PASS:Python AST, CLI help/choice inspection and git diff whitespace checks.
- NOT RUN in task48: Docker build/run, native file/database E2E, real layer inspection, arm64, GHCR/signing or production compatibility/rollout. Lead owns any necessary final build and acceptance; earlier image tests are historical evidence, not a fresh task48 run.
- No task48 containers, background jobs or persistent terminals were started. Synthetic unit directories are self-cleaning.
