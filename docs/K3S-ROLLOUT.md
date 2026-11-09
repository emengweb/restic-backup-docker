# K3s deployment and automatic-recovery integration (NOT deployed)

## One image in two phases

Use the exact same tested image digest for recovery init and backup sidecar. Init command `restore-init` completes before the main DB/application starts. Sidecar command `schedule` shares no API token, does not need to discover a Pod or contact Kubernetes; it connects through the configured DB Service or localhost for same-Pod databases. SOURCE data is mounted read-only for file snapshots; database logical sidecar does not mount PGDATA. The recovery init has a write mount of the future main volume and independent scratch.

Preserve existing env/envFrom Secret refs and cron/window values while adding dataset/engine/version/volume limits. `DATASET_ID` must survive Pod/node/controller recreation; do not derive it from Pod/controller UID. A repository per project can default to its repository-derived identity. Shared repositories must set explicit identity.

For an empty target, restore-init automatically selects latest complete compatible candidate across native logical/physical formats, downloads and verifies, restores native DB into private same-volume stage, cleanly stops the helper, and publishes. An existing valid nonempty volume is skipped without contacting Restic. Owned interrupted stage resumes the same snapshot; completed publication works offline. Never replace this with raw restore-to-root of the most recent mixed-format snapshot.

## Volume and scheduling requirements

An emptyDir is new on Pod recreation. A hostPath is node-local and may already hold unrelated data on another node: restore must preserve nonempty content; allow targeted marker validation rather than assume any path is yours. local-path PVC nodeAffinity may prevent cross-node scheduling before init can run. This runner cannot relocate a bound local PV. Deployment/controller/CSI policy must provide the eligible target volume; preserve old PVs and never delete/rebind solely because a node is lost.

The target mount root inode is not renamed: publication uses same-volume no-clobber top-level renames with a durable journal and atomic per-entry transitions. This is NOT an atomic whole-volume swap. The main app must not run until init returns0. Other generations/writers need fencing: a K8s Lease is coordination, not power/storage fencing of a partitioned old node. RWO does not ensure one Pod. Keep single-writer/old-generation termination or RWOP where available, and do not promise split-brain prevention solely from Restic locks.

No new failure path should initialize an empty business database. New deployment with no snapshots is a separate explicit provisioning action. Existing FORCESYNC cannot authorize deletion of unrelated data; forced replace needs a distinct operator-managed staging/switch, not blind rm-rf.

## Security and capacity

Keep automountServiceAccountToken=false when none of the workload containers requires API; do not blindly remove an existing app requirement. Restore needs chown/setuid/setgid/fowner/dac_override inside its own container; do not use privileged/hostPID or mount the Docker socket/host root. Runtime must be patched and reviewed. Tools can be baked in one immutable image, so no tool-init Secret or write access is needed.

Configure BACKUP_MAX_BYTES and scratch/target free-space budgets from each dataset, rather than use Affine64MiB. Native PG/MariaDB datadir expansion exceeds compressed dump size; include redo/WAL/two-stage overhead. Runner monitors aggregate scratch/payload, uses file size and job limits and Restic size preflight; polling can overshoot and is NOT a filesystem quota. K8s emptyDir.sizeLimit is eviction accounting, not a synchronous hard quota. Use real storage quota/capacity isolation where a hard bound is required.

Backup freshness is a separate status/metric; do not gate all DB endpoints on backup success. All normal DB readiness probes remain owned by the DB/application. Same-Pod localhost retries avoid Service NotReady bootstrap loops.

## Repository maintenance

check/prune are whole-repository operations. Native Restic locks serialize individual commands across nodes; local runner flock only handles local tasks. An exclusive-maintenance deployment policy must fence writers over multi-command repair/prune transactions; a boolean is an assertion, not proof of fencing. Default retention is dry-run, no automatic destructive repairs/unlock/init. Operator-supplied retention proof records stable observation interval, unresolved failures and exact recently restored snapshot IDs; it must be persisted outside ephemeral Pods and reviewed, not fabricated by elapsed time.

Seven stable days start after first real snapshot restore acceptance for each rollout. Only then enable configured whole-repo cleanup policy; preserve legacy and last verified complete recovery points. Pack repair or snapshot rewriting can lose data and remain prohibited by default. S3 object-version repair requires a separate privileged verified policy, not ordinary backup keys.

## Migration sequence after separate approval

1. Inventory current controller/volume/Secret refs/version/window and backup history without secret output.
2. Test actual legacy restore and compatible native backup/restore in shadow storage, including extension/libc/architecture constraints.
3. Preserve current Pod/PVC/data and repository history. Patch entire reviewed spec with resourceVersion guard; do not replace only partial container arrays with merge patch.
4. One canary PG job, restore-init and sidecar deployed together after current backup, planned brief singleton restart. Validate source and restored DB/read/write, native snapshot and RPO; rollback before new business writes.
5. Seven-day stable observation, then other PG and MariaDB sidecars; production MariaDB upgrades are still a separate later operation.
6. EmptyDir-based apps (Grafana/Openlist/KeyDB/Vikunja etc.) must be quiesced/backed-up/restore-tested BEFORE a template update destroys local volume. They are not trivial file-sidecar canaries.

## Test status

Local container fixtures exercise new empty mounts, changed hostname/Pod labels and native startup; they do not emulate a real two-node K3s failure, CSI reattachment or network partition. No production-cluster E2E was authorized in this phase. CI/container passes do not assert cross-node storage/controller fencing has been tested. Add a dedicated disposable two-node K3s acceptance run before rollout.
