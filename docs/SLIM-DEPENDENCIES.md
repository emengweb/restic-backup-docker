# Slim unified runtime dependency closure

## Authorization and baseline

- 2026-11-09 11:00 Asia/Shanghai: Lead delegated task44 after user requested slimming the approximately808MB unified image. Local implementation and isolated filesystem/ldd analysis only. No production, push, registry publication, private data or credentials.
- Write scope: scripts/collect-runtime.py, tests/test_runtime_closure.py and this document only. Lead owns Dockerfile, JIT configuration, selftest/harness changes, build and acceptance. Rollback/review owner: Lead; preserve original full image.
- Baseline available bytes56,961,445,888. Expected source edits<100KiB; collector later materializes one fresh build-stage rootfs (size measured by Lead), never source volumes or existing backups. Backup ID: not applicable to source-only synthetic work. Stop on missing required dependency, old/unverified runtime, unsafe target or unknown source artifact.
- Metadata-only inspection uses approved private daemon unix:///root/project/k3s/maintenance-2026-11-06/restic-k3s-unified/test-docker/docker.sock and patched runc1.4.2, image k3s-restic-unified:local-dev, network none/read-only root/cap-drop-all/256MiB/0.5CPU, exact names slim-closure-*. Containers are removed after each inspection. No DB server/build was started by this teammate.

## Collector interface

`python3 scripts/collect-runtime.py /opt/runtime-rootfs` or `--output /opt/runtime-rootfs`. Output must be a new or empty absolute directory; existing nonempty targets are rejected, never deleted. `--dry-run` inspects dependencies without copying. Default source is the current build-stage root filesystem, not the host. Stdout is aggregate JSON only. Missing required resource, unresolved ELF dependency or unsafe link fails nonzero.

## Explicit closure and intentional capability exclusions

The collector starts with 75 explicit shell/database/Python/restic/rsync/archive/locale/privilege/cron utilities plus 23 PostgreSQL tools for each major. Both PostgreSQL16/17 servers, initdb, clients, pg_dump/restore, pg_basebackup/verifybackup and MariaDB11.8 server/client/dump/admin/install/backup remain together in the sole final image. No per-engine production images and no server removal.

Every selected ELF (including dlopen plugin seeds) has its native ldd closure recursively collected. Interpreter/loader symlink chains are preserved. All non-JIT PG shared objects/encoding conversions and PostgreSQL share/initdb catalogs/SQL/tsearch/timezone/extension resources remain. MariaDB share path was actually verified as /usr/share/mariadb; its complete ~1MiB plugin tree, auth_pam helper included, remains.

Non-ldd seeds: whole Python3.11 stdlib and lib-dynload including _ssl/_ctypes; C.UTF8 and generated en_US.UTF8 locale resources; IANA timezone database; CA bundle/cert symlinks/OpenSSL config and provider modules; NSS libraries/config; gconv; PAM modules/config; available GSSAPI/Kerberos/SASL plugin trees. passwd/group are preserved; shadow, TLS private keys, real databases/credentials are not copied. Only an empty private TLS directory is created.

Intentionally omitted: LLVM/libz3 dependency chain, PG llvmjit modules/bitcode/pgxs/static archives, Galera wsrep library, compiler/build/package-manager binaries and caches, most unused MariaDB utilities, Python bytecode/cache/test payload. No ELF stripping is performed; selected native bytes remain hash-identical to the pinned build-stage artifacts. Lead must keep temporary helper/E2E jit=off. Consequence: PostgreSQL JIT and Galera/wsrep clustering are unavailable in this backup-helper image. This is not a general database-server replacement or a claim that physical source clusters using JIT/Galera are accepted.

Usrmerge-safe copy order: canonical real directories, symlinks, then regular files. Absolute links resolve within the source-root model, never through host filesystem fallback; no directory creation follows /bin or /lib aliases. uid/gid/mode are preserved, including dedicated DB accounts; required tools/ELF dependencies, unresolved links, missing selected package copyrights and nonempty output fail closed. An interrupted partial output is retained, not silently wiped/reused.

## Traceability and license inventory

/usr/share/k3s-restic/runtime-files.json records each selected path/type/bytes/mode/uid/gid, file SHA256, symlink target and original build-package owners when resolvable. /usr/share/k3s-restic/slim-runtime.json records schema1, postgres_jit/galera exclusions, file count/bytes and explicitly partial source-package scope. Metadata outputs exclude their own hashes to avoid recursion.

Original build-stage packages.txt becomes builder-packages.txt with a prominent BUILD STAGE ONLY label; the final rootfs does not contain a misleading installed-package status database that claims omitted LLVM/Galera packages are installed. dpkg-query --search maps actual selected paths (including Debian usrmerge aliases); explanatory diversion lines are not package names. Selected package copyright files plus common-license texts are retained in /usr/share/doc and /usr/share/common-licenses. Non-dpkg restic source license is supplied by Lead in the build stage; upstream checksum locks remain. Partial package copying is not represented as complete binary package installation; runtime-files.json is the authoritative file evidence.

## Actual verification and remaining acceptance

- PASS: metadata-only inspection of approved immutable full image, with no DB startup, build, production/network access or writable host mounts. Initial cap-drop-all read failed at mysql-owned0700 auth_pam_tool_dir; Lead explicitly approved only DAC_OVERRIDE for subsequent read-only analysis. No plugin was removed to evade permissions.
- PASS: actual collector dry-run on old tested image, including recursive native ELF dependencies and selected license mapping: files3675, directories327, symlinks762, ELF736, selected regular bytes284,807,436 (~271.6MiB; not compressed final image size). Lead independently repeated this exact aggregate and owns final build.
- PASS: 21 synthetic unit tests (parser missing/static/recursive libraries, rejected excluded dependency, source-only absolute-link resolution/no host fallback, usrmerge/symlink loop/escape, output preservation, modes/owners/hashes, required globs/tools/interpreters, special-file rejection and truthful slim marker).
- Initial dry-run issues were diagnosed and fixed: data-only stdlib shebang examples are not executable runtime dependencies; dpkg diversion explanatory text is not a source package. Real missing native dependencies still fail.
- Lead reported slimv3 build success/image-selftest PASS (SSL CA/NSS/locale/timezone/ctypes included), actual image bytes285,825,533. First E2E invocation failed because the explicit CI harness entrypoint was not in the selected closure; collector now includes only that synthetic script and hashes it in runtime-files.json. This was a historical intermediate result; final Lead rerun below supersedes this pending status.
- Final slim scratch image build/start, all six real container E2E cases, arm64 execution and GitHub/GHCR/SBOM publication are NOT RUN by this teammate. Lead owns those steps and must record actual size/image ID and acceptance. File closure/ldd proves neither database restore correctness nor runtime dlopen behavior by itself.
- Final Lead verification 2026-11-09 11:17 Asia/Shanghai: final slim image sha256:5ac2bc34c55122563d46183a1be9d1f5044f436d23b6e90d47f643e692c6f842, 285,856,254bytes versus808,024,689bytes (-64.62%). All162 source/real-restic tests and7 no-source-bind container E2E cases PASS/exit0; gzip archive107,709,540bytes (-64.33%). Includesfiles,PG16,PG17,vector0.8.2,PGphysical,PGdual,MariaDB11.8. No arm64/production/publish claim. Lead verified final source module hashes match image, no apt/dpkg/LLVM/Galera/private TLS key present.
- All slim-closure-* metadata containers were removed after inspection. This teammate created no persistent Docker resources, background jobs or terminals; source tests use self-cleaning synthetic temporary directories.
