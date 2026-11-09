# Unified backup image and CI

## Local authorization and evidence ledger

- 2026-11-09 09:43–10:00 Asia/Shanghai: user authorized **local implementation and isolated synthetic tests only**. No production access, no push, no registry publication executed. Rollback/review owner: Lead.
- Targets: this repository’s Dockerfile, build-context allowlist, image self-test, container E2E and GitHub workflow only; no source volumes, existing snapshots, credentials or Kubernetes resources. Expected source edits <150 KiB; baseline filesystem available 60,640,825,344 bytes. Backup ID: not applicable (synthetic-only source changes).
- Stop conditions: unverified runtime, missing upstream version/checksum, failed native dump/restore, insufficient headroom, unexpected host mount/network or any production scope. No container/build runtime may be used before Lead approves its exact daemon/runtime.
- Lead confirmed system Docker uses unpatched runc 1.1.5: **never use that daemon**. A network-none container is not a workaround for a vulnerable host runtime.
- 2026-11-09 10:04 Asia/Shanghai: Lead verified and approved a private local daemon at `unix:///root/project/k3s/maintenance-2026-11-06/restic-k3s-unified/test-docker/docker.sock`, separate containerd namespace `dsh-restic-test`, default runtime `patched` using runc1.4.2. Lead owns daemon/build cleanup and temporarily delegated exclusive synthetic E2E execution to backup-image. Existing test image `k3s-restic-unified:local-dev` ID prefix `8718fad7889c`; only repository Python source is bind-mounted read-only for diagnosis, never production mounts/credentials. Available bytes before E2E: 59,773,100,032. Runtime hard limits are enforced by the harness. Test container names `backup-image-debug-*` are created and removed by backup-image; no image/pull/push/build is performed by this teammate.

## One image, not one image per engine

The sole final image contains PostgreSQL **16.15 and 17.11**, each with server, client and **pgvector 0.8.7**, MariaDB **11.8.9** server/client, restic **0.19.1**, Python 3.11, CA certificates, rsync, cron, util-linux/runuser, and UTF-8 locales. No engine-specific production image is emitted. Engine selection is runtime configuration. PG binaries use `/usr/lib/postgresql/{16,17}/bin`; MariaDB uses Debian native `/usr/bin` and `/usr/sbin` paths.

Entrypoint: `python3 -m k3s_backup`; commands: `backup`, `restore-init`, `check`, `prune`, `health`, `schedule`. No database daemon is started at image build or ordinary image startup. `restore-init` starts an isolated temporary native server and shuts it down before publishing the datadir. This is a logical restore, **not** pg_upgrade or validation of a live physical copy.

Postgres uid/gid is 999, mysql uid/gid 998 in this image. Engine restore configuration may explicitly request the destination database uid/gid; those must match the real application image/PVC owner. The image defaults to root for ownership-preserving file backup and dropping privileges for database helpers; run non-root when appropriate. Production security contexts are not approved by this implementation task.

## Verified supply-chain inputs (2026-11-09)

### Base and Debian packages

- [Docker official Debian source declaration](https://raw.githubusercontent.com/docker-library/official-images/master/library/debian) identifies `bookworm-20261104-slim` (Debian 12.15).
- Docker Registry v2 manifest for `library/debian:bookworm-20261104-slim`: index `sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc`; amd64 `sha256:c67e92c8099ba22853ab4cfda7cfb494d253d6012ca82dee98ba8976772af018`; arm64/v8 `sha256:ec8534cda2cb30f29f44f23e135297af7a1496f2990fd8e4e84aa36b081a757a`. Registry manifest bytes were SHA-256 checked without pulling image layers.
- [Debian fixed snapshot](https://snapshot.debian.org/archive/debian/20261108T000000Z/dists/bookworm/Release) and [Debian security snapshot](https://snapshot.debian.org/archive/debian-security/20261108T000000Z/dists/bookworm-security/Release): both architectures’ Packages.xz were read. Direct package versions in the Dockerfile were checked against those indexes. APT still verifies Debian archive signatures and package hashes. Only snapshot expiry checking is disabled; signature checking is **not** disabled. HTTP bootstrap transport is acceptable here only because Debian signed indexes/hashes establish authenticity; restic/PG downloads use HTTPS with a CA bundle.
- Transitive Debian packages are resolved exclusively from that timestamp, not a rolling mirror. The complete installed package/version inventory is retained inside the built image at `/usr/share/k3s-restic/packages.txt`. Fixed inputs do not prove the image is free from CVEs: scan and review before any production use.

### PostgreSQL / pgvector

[PGDG amd64 Packages.gz](https://apt.postgresql.org/pub/repos/apt/dists/bookworm-pgdg/main/binary-amd64/Packages.gz) and [PGDG arm64 Packages.gz](https://apt.postgresql.org/pub/repos/apt/dists/bookworm-pgdg/main/binary-arm64/Packages.gz), release date 2026-11-05: verified exact versions and per-package SHA256 for PG16.15/17.11 (`-1.pgdg12+2`), pgvector0.8.7 (`-1.pgdg12+1`), common293 (`.pgdg12+1`), and libpq18.6 (`-1.pgdg12+2`). The Dockerfile embeds every PGDG download checksum for both architectures; it installs only those local `.deb` files, with no rolling PGDG APT source. The libpq18 client library is shared, not a third database server. A missing/changed artifact fails the build instead of selecting another version.

Upstream index retrieval over HTTPS and committed checksums were verified; a separate detached PGDG index-signature ceremony was not performed locally. Debian dependency signatures are checked by APT.

### MariaDB

The initial Debian 10.11 candidate was rejected before building: it is not the requested production recovery target (source 11.5.2, target 11.8.9). [Official MariaDB 11.8.9 Debian archive amd64 index](https://archive.mariadb.org/mariadb-11.8.9/repo/debian/dists/bookworm/main/binary-amd64/Packages.gz) and [arm64 index](https://archive.mariadb.org/mariadb-11.8.9/repo/debian/dists/bookworm/main/binary-arm64/Packages.gz) were read and compared to the current official 11.8 repository. Exact `1:11.8.9+maria~deb12` server/client/backup/common/cores/libmariadb3 plus Galera `26.4.27-deb12` checksums are embedded in the Dockerfile. This is native Debian bookworm packaging, not Ubuntu binaries copied without libraries. APT resolves Debian dependencies only from the frozen signed snapshot. Source11.5.2→target11.8.9 production compatibility remains unverified until an authorized exact-source rehearsal; same-target synthetic E2E is not that proof.

### restic

[Official restic v0.19.1 release](https://github.com/restic/restic/releases/tag/v0.19.1) published 2026-07-05. [SHA256SUMS](https://github.com/restic/restic/releases/download/v0.19.1/SHA256SUMS) SHA256 `fb520966ee01d2a3d4219c66762c66efa56300833b6f639f36082b7462f91cb8`; both entries matched GitHub release asset digests:

| Architecture | SHA256 of compressed binary |
| --- | --- |
| amd64 | `f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c` |
| arm64 | `a5f64aaab53d51e311fa3829124c5b703f2d14cf187d8640b6be3b2b49376465` |

The build verifies the pinned checksum **before** decompression/execution. No claim of locally verified detached restic release signature is made.

## CI and test execution

## Required CI and publication gates

The old untested publisher is replaced in place. PR/branch/manual CI runs Python3.11 unit + real synthetic local-restic integration (any skip fails CI) and native amd64/arm64 unified-image E2E. Unknown/old runc below reviewed1.4.2 fails with NOT RUN. No QEMU/per-engine production images.

All external Actions use full40-character official commits resolved2026-11-09. Buildxv0.38.2, BuildKit sha256:c5f8862f52499773af85bc07695b2379ef08e8002352b0df2c4864c91d78dabe, Syftv1.57.0 and Cosignv3.1.4 were publicly verified. Publication defaults OFF: a future owner must set ENABLE_GHCR_PUBLISH=true and protect ghcr-release. Only semver tag PUSH after unit + both architecture E2E succeeds can publish. PR/manual cannot publish; nothing was pushed/tag-triggered by this task.

CI retains exact tested image archive/ID, E2E result, SPDXSBOM and actual Buildx maximum provenance. Release loads/pushes that same ID, never an untested rebuild. Artifact/SHA checks guard handoff. Cosign signs per-architecture digests and SPDX/SLSA predicates, then signs combined index. Docker-load exporter loses OCIattestations; real build metadata is retained and attested after publication instead. GitHub/GHCR/SBOM/signing actual execution NOT RUN locally.

## E2E requirements and coverage

Harness requires explicit --runtime-reviewed, --image and --docker-host; otherwise exit2 / NOT RUN. No pull/build/push, no socket mount or host AWS/database/restic/Kubernetes/proxy forwarding. It pins execution to existing image ID. Network none/no default route, read-only root, private PID/IPC, no host namespaces/production mounts, memory768MiB/no swap, CPU0.5, PID192, private512MiB /tmp + separate384MiB /backup and /data tmpfs. Fixture archive/file cap256MiB.

Capabilities dropped except CHOWN,DAC_OVERRIDE,SETUID,SETGID,FOWNER. FOWNER is needed for chmod after chown; DAC_OVERRIDE alone insufficient. Engine signals owned child as DBUID, no CAP_KILL required. Kubernetes init helpers need these or a separately tested non-root arrangement; this documents requirements, not rollout authorization. No privileged/hostPID.

Cases: files latest/recreatedPod/symlink/mode/nonempty restart; PG16/PG17 logical twoDB JSONB/bytea/vector/FK/index/sequence/locale/owner/grants plus native restart/write; PG17 vector0.8.2 exactidentity; PG17 pg_basebackup+pg_verifybackup physical; MariaDB11.8.9 native twoInnoDB databases/globals/grants/JSON/blob/FK/view/trigger/routine/event+restart. Failed exports cannot create snapshots/replace last_success. Restic check reads all synthetic data. All resources are test-created and removed; no real backups.

Added exact vector0.8.2 SQL31171bytes SHA256 7fb5bb279ef83bf9204bfac7405bb5c9a05e49f5ac7d64d1eb4464d103b80f32 to both majors, preserving PGDG0.8.7 update chains. [Official source](https://raw.githubusercontent.com/pgvector/pgvector/v0.8.2/sql/vector.sql) fetched+hashed; native compatibility still needs its actual E2E, not just file installation.

## Actual teammate verification and limits

- PASS: public base/package/checksum/Actions/builder metadata for both target architectures. No invented pins.
- PASS: PythonAST/nested diagnosticAST, shsyntax, workflow YAML, fullSHA pin checks, all9 Bash block syntax,3 harness isolation unit tests, missing-opt-in exit2 and git diff whitespace.
- Lead built amd64 unified image reported807MB. Teammate diagnostic runs used image sha256:8718fad7889c645c4dede11276c991ec680bc6522eab918931fb58dd252f58fb plus current Python sources read-onlybind. This is NOT frozen/no-bind image evidence.
- PASS: PG16 full native logical/restic/restore/restart/invariants/write/clean shutdown/check. That pass included CAP_KILL before sameUID signal fix; final no-KILL rerun belongs to Lead.
- PASS: PG17 full same loop without CAP_KILL. Files PASS reported by Lead initial image.
- Maria native globals+DB dump PASS; teammate latest restore FAILED importglobals ERROR1698 / SQLSTATE28000 after installer/rootbootstrap/bootstrap seed query+removal succeeded. Exact safe stage/line handed to Lead. Engine later adjusted sequence to preserve fresh system accounts until after import; teammate has not retested. No Maria restore success claimed here.
- Earlier failures investigated: missingFOWNER; crossUID kill replaced sameUID; install-db root@containerhostname invalid under skip-name-resolve (fixture-only accounts cleaned; product failclosed); redo100MiB exceeded old64MiB fixture (helper8MiB + fixture256MiB).
- Exact vector0.8.2/newphysical/arm64/final frozenimage/GitHub/GHCR/SBOM/signing NOT RUN by teammate at handoff. Lead final results supersede only separately recorded matching entries.
- All teammate backup-image-debug-* containers removed by exactname, exclusive runtime returned Lead. No backgroundjobs/terminals started by teammate. Daemon/images are Lead-owned and not deleted by teammate.

Synthetic same11.8 tests do not prove source11.5.2->11.8.9 production/application compatibility, realpermissions/locale/bytebudgets or rollout. No production operation, secretread, push or publication occurred.

## Lead final frozen-image verification (reported 2026-11-09 10:39 Asia/Shanghai)

Lead reports that the final v6 frozen unified image completed the full six-case container suite **without source bind mounts**, overall **exit0 / PASS**:

| Case | Final result |
| --- | --- |
| Files latest restore | PASS |
| PostgreSQL16 logical | PASS |
| PostgreSQL17 logical | PASS |
| PostgreSQL17 exact pgvector0.8.2 | PASS |
| PostgreSQL17 physical | PASS |
| MariaDB11.8 logical | PASS |

This result supersedes the corresponding earlier handoff limitations and MariaDB1698 failure above; those entries remain historical diagnostic evidence, not the final status. Execution and final image review were performed by Lead on the approved isolated patched runtime, not independently rerun by backup-image. Lead records the final image ID and any candidate artifact separately; no final image digest is invented here.

Still **NOT RUN / not established by this result**: arm64 runtime acceptance, GitHub Actions execution, GHCR publication, SBOM/signature publication, exact production-source rehearsal and production rollout. The current no-production/no-push authorization remains unchanged. No image/test/CI source code was changed for this documentation update.
