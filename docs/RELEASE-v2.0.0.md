# v2.0.0

First versioned release of the K3s automatic latest-recovery runner. Two variants only: default all-in-one and opt-in lite (no database binaries). Native amd64/arm64 builds/tests, content-identical shared base, SPDX SBOM, provenance and keyless signatures are required before publication.

This is a major release relative to the legacy Node/shell entrypoint: Kubernetes `command` overrides must be migrated with image changes; do not simply substitute the image and leave `restic restore latest --target /` or `/bin/backup` commands. Backup sidecar uses `python3 -m k3s_backup schedule`; restore init uses `python3 -m k3s_backup restore-init`. Keep original repository/password/IAM/cron/window; set compatible database identity/version, separate scratch and source/target mounts.

No production acceptance is inferred from synthetic CI. PG16/17 and MariaDB11.8 helpers are present; a MariaDB11.5 datadir must not be handed to an 11.8 helper without explicit migration, and then returned to an unchanged11.5 main server. Existing legacy live PGDATA snapshots need actual compatibility/WAL validation. Nonempty directories are preserved; empty directories need a valid compatible recovery candidate before the main app starts.

Metadata extraction is file-based to avoid environment argument limits. Generated build-only snakeoil certs are excluded while trusted CA certificates remain. Cosign3 installation uses the reviewed bundle-capable pinned installer, not the old installer requiring detached .sig assets removed in v3.

History retention/pruning remains separately gated by seven days stable observation and concrete restore verification. Do not delete old MinIO snapshots solely because the Restic backend moved to RustFS.
