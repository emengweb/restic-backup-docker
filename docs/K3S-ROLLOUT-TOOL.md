# Offline per-project K3s rollout tool

The tool in [k3s-rollout.py](../scripts/k3s-rollout.py) **never invokes kubectl, talks to Kubernetes, reads a Secret, applies a patch, scales a workload, commits, or pushes**. The Lead/operator performs all live actions. Tests contain synthetic objects only.

## Backup first (safe interface available independently of patch generation)

Pass exactly one Deployment/StatefulSet/DaemonSet and all **referenced, same-namespace** ConfigMaps as JSON. Accepted input is a raw controller, a Kubernetes List with one controller plus its ConfigMaps, or this envelope:

~~~json
{"controller": {"kind": "Deployment", "...": "full original object"}, "configmaps": []}
~~~

Do **not** include Secret objects. Secret references (name/key only) are inventoried from env, envFrom, projected volumes and imagePullSecrets; values are neither requested nor accepted. An existing controller can contain literal passwords and ConfigMaps can also contain sensitive configuration: therefore **the entire input, backup and patch are private**, even if a particular field looks harmless.

~~~sh
# Lead only: pipe a previously collected single-project JSON bundle, not Secret JSON.
python3 scripts/k3s-rollout.py backup --input - --backup-root /absolute/root-private-parent
~~~

The parent must be root-owned mode0700; only its final path component may be created. All ancestors are opened without following symlinks. Root is required; no permission escalation is attempted. Every backup creates a new UTC+random ID directory, mode0700, without reusing/overwriting an earlier backup. Within it:

- controller.json: complete original controller, including old init, backup sidecar, env/envFrom, scheduling, mounts, labels and desired replicas;
- configmaps.json: complete supplied referenced ConfigMaps;
- index.json: completion record, SHA-256 of the two exact files, controller UID/resourceVersion/generation/desired replicas, Secret name/key references, and command/image/env/config digests.

Files are mode0600, written to exclusive temporary names, fsynced, atomically hard-linked without overwrite, then the directories are fsynced. index.json is committed last. A crash may leave an incomplete new backup directory; **never treat it as complete** or delete earlier backups. The CLI does not delete incomplete directories. It does not dump JSON, commands, env literals, repository URLs, ConfigMap values, Secret references, or tracebacks to stdout. Output contains fixed codes, counts, digest-only configuration summaries and a generated backup ID. Keep both the ID and index_sha256 receipt.

Missing referenced ConfigMaps do not prevent preservation of the original controller, but produce BACKED_UP_NO_GO/exit3. Complete the inventory and make a **new** backup before patching. The tool refuses unrelated or cross-namespace ConfigMaps and all Secret payloads.

## Default plan (no approval or patch)

~~~sh
python3 scripts/k3s-rollout.py plan --input /absolute/operator-supplied-input.json
# Optional: take a backup while planning.
python3 scripts/k3s-rollout.py plan --input - --backup-root /absolute/root-private-parent
# Optional: inspect an explicit non-secret mapping, without fabricating evidence.
python3 scripts/k3s-rollout.py plan --input - --plan-input /absolute/mapping.json
~~~

No command defaults to apply. An optional mapping contains dataset_id, engine, sidecar_name, init_name, data_volume, recovery_data_path, source_data_path (files). A MAPPING_REVIEWED_NOT_APPROVED result is **not** authorization, restore acceptance, signature validation, or a promise of production compatibility. Unknown lifecycle hooks, ambiguous data/mounts, missing ConfigMaps and opaque Secret envFrom conflicts are NO_GO/manual-review findings. Existing credentials, cron/timezone/window/database config must be preserved, not normalized from guesses.

## Approval-bound patch template (generated only, never applied)

~~~sh
python3 scripts/k3s-rollout.py patch-template --input /absolute/current-project.json \
  --backup-dir /absolute/root-private-parent/EXACT-BACKUP-ID \
  --backup-index-sha256 RECEIPT_SHA256 \
  --approved-evidence /absolute/operator-evidence.json
~~~

Only an exact current-object match to a complete private backup is accepted. The tool re-hashes all backup files and the supplied SHA receipt, compares the entire controller/ConfigMaps, and checks that every referenced ConfigMap was saved. Stale resourceVersion, altered backup, changed desired replicas or foreign controller UID are rejected. Re-read current public API metadata and re-backup after any controller change.

Evidence is an **operator-reviewed assertion record**, not a cryptographic signature implementation. The tool never fabricates a pass or fetches registry proof. The operator must independently verify the image signature/attestation and real isolated restore, then record their evidence hashes. A boolean alone is not independent proof. **The current offline schema does not itself bind actual backup/restore records to a snapshot, source PVC identity or freshness/RPO. Before any live patch, the outer Lead/operator gate must verify a real current data backup and exact-snapshot isolated restore for that project/source with the new image; restore_passed=true plus an arbitrary hash cannot authorize rollout. Unknown/stale/stopped-source recovery evidence stays NO_GO without separately reviewed acceptance—never start a stopped workload to manufacture proof.** A separately reviewed follow-up will add structural data-proof binding; it is not part of these quick P1 fixes. Required current fields:

~~~json
{
  "schema_version": 1,
  "approved": true,
  "controller_sha256": "64 hex: exact current backup controller digest",
  "uid": "exact controller uid",
  "resource_version": "exact current resourceVersion",
  "backup_index_sha256": "64 hex: receipt index digest",
  "desired_replicas": 0,
  "signature_verified": true,
  "signature_evidence_sha256": "64 lowercase hex",
  "restore_passed": true,
  "restore_evidence_sha256": "64 lowercase hex",
  "public_image": "ghcr.io/owner/repository@sha256:64-lowercase-hex",
  "dataset_id": "stable-project-dataset",
  "engine": "files",
  "sidecar_name": "existing-backup-container",
  "init_name": "existing-restore-init",
  "data_volume": "existing-data-volume",
  "source_data_path": "/data",
  "recovery_data_path": "/data",
  "work_volume": "restic-work",
  "work_path": "/backup",
  "scratch_size_limit": "2Gi",
  "backup_max_bytes": 1073741824,
  "backup_min_free_bytes": 25769803776,
  "resources": {"limits": {"cpu": "500m", "memory": "4Gi"}}
}
~~~

Use actual per-project capacity values, not this example's values. The explicit 2Gi shared work / 4Gi memory / 1Gi maximum / 24Gi free-space example is not a tool default or a capacity proof; a 558MB restore cannot fit in a 64Mi work volume. emptyDir.sizeLimit is accounting/eviction rather than a synchronous filesystem quota and does not preallocate the full limit. Size scratch from the actual archived bytes, extraction/verification copies and database expansion with measured headroom. DB engine postgres/mariadb also requires target_major. No MongoDB adapter or unreviewed physical/dual profile is generated. PostgreSQL PGDATA layouts, unknown extra mounts or hooks require manual review rather than relabeling raw PGDATA as a logical archive target.

Secret envFrom cannot be inspected for collisions without reading Secrets, which this tool must never do. By default it blocks automatic patch generation. Only after explicit operator review may evidence set both opaque_secret_envfrom_reviewed=true and unresolved_secret_env_conflicts_accepted=true. These acknowledge the remaining unknown risk; they **do not prove the Secret lacks a collision**. Exact envFrom references are preserved unchanged. Prefer reviewed explicit per-key refs for deterministic collision checks. Unknown/duplicate known env keys or mismatched init/sidecar envFrom are still rejected. If the selected old init/sidecar has any command/args, evidence must also contain legacy_commands_reviewed=true plus the exact selected pair's legacy_commands_sha256 from plan --plan-input. This binds operator review to the old shell/hooks without printing them; unknown hook environment variables or lifecycle/probe hooks remain NO_GO rather than being silently dropped.

### Phase-specific file paths and root security context

The runtime prefers RECOVERY_DATA_PATH over DATA_PATH even for schedule. Therefore files sidecar **both aliases point to source_data_path**, while restore init **both aliases point to recovery_data_path**. A different container mount path for the same source volume is supported; a sidecar never receives the init target alias by common-env merging. Existing literal or ConfigMap path keys must already agree with their own phase, otherwise generation is NO_GO rather than silently replacing a conflicting value/ref. Original credential, cron, timezone and database settings still merge verbatim. A source/target-different regression test round-trips generated env through the actual Config.from_env.

File sidecar mounts with subPathExpr or mountPropagation are rejected, including explicitly present empty/None values. Dynamic tenant expansion or mount propagation cannot be treated as a stable subtree-to-volume restore mapping. Fixed subPath still must exactly match the init and application subPath; no whole-volume recovery from an unmatched subtree is generated.

Root helpers must not inherit an effective runAsNonRoot=true. An explicit container setting takes precedence over the Pod setting. When either selected tool container effectively has runAsNonRoot=true, patch generation returns root_restore_security_context_requires_review unless approved evidence explicitly sets root_context_override_reviewed=true. The generated patch sets runAsNonRoot=false only on the two tools along with runAsUser/runAsGroup0; the Pod and main application security contexts are preserved unchanged. This is a reviewed privilege requirement, not a request for privileged, host namespaces or cluster access.

### StatefulSet volumeClaimTemplates

For a StatefulSet, one exact-name filesystem volumeClaimTemplate is the effective data source even when spec.template.spec.volumes has no entry of that name. The tool preserves the claim template, all original PVC semantics and all unrelated template volumes; it never synthesizes a hostPath or substitutes a similarly named unused volume such as data-o. An omitted volumes array is supported. The generated scratch name is checked against both template volumes and implicit claim names to prevent an accidental shadowed scratch mount.

A same-name template volume is overridden by the StatefulSet claim template. This unusual case is NO_GO unless approved evidence sets claim_template_shadowed_volume_reviewed=true. Even after review the original shadowed volume is preserved as-is and is **not** treated as the restored source; hostpath_reviewed is not used to reinterpret a shadowed hostPath. Duplicate exact-name templates, block-mode claims, mismatched claim names and claim templates on non-StatefulSet controllers are rejected. plan --plan-input reports data_volume_source=claim_template for the effective source without printing a storage path.

The generated private patch:

- tests controller UID, resourceVersion, old desired replicas and the full old template;
- preserves all other application containers and template labels/configuration;
- replaces the reviewed entire template, avoiding lossy array merge patches;
- sets restore init command to python3 -m k3s_backup restore-init and sidecar to python3 -m k3s_backup schedule;
- uses the same immutable public digest for both phases;
- preserves original literal and referenced env, Secret refs, cron/timezone/window/database configuration;
- adds one **new shared scratch emptyDir**, separate from and nonoverlapping with data, mounted in both init and sidecar;
- requires recovery_data_path to equal the existing writable init mount; requires application/restore subPath agreement; keeps file source read-only;
- removes raw data mounts from a logical-DB sidecar; rejects unknown mounts rather than guessing;
- preserves stopped desired replicas0 and explicitly includes a replicas0 replacement; never scales to1;
- adds only scoped root ownership/setuid capabilities, not privileged/host namespaces; requires a patched, separately approved runtime.

Output is PATCH_GENERATED_NOT_APPLIED with only the private filename, patch/evidence digests and counts. The patch itself includes old template and potentially credentials in env, so it is **never stdout**. It is another exclusive mode0600 file inside the verified backup directory. Lead must review privately before using any patch-file operation; do not print diffs containing literals.

## Limits and stop conditions

Backups preserve configuration, **not PVC/emptyDir data or a verified application recovery point**. Secret values are deliberately not backed up. Retain originals and never claim this restores lost credentials. No data source is read by this tool. A stopped app remains stopped; emptyDir workloads need a current verified data backup before any template rollout destroys their existing Pod-local volume. The tool neither implements controller fencing nor a Kubernetes transaction spanning reads/backups/apply. JSON Patch tests prevent stale application only when Lead actually uses the generated tests unchanged. No automatic rollback, PVC deletion, Secret recreation, repository init/repair/unlock/prune, or endpoint mutation is provided.

Run local synthetic tests:

~~~sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -p test_k3s_rollout.py -q
~~~
