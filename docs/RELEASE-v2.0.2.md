# v2.0.2

CI supply-chain transport fix. v2.0.0/v2.0.1 tags remain immutable and unsuccessful publication attempts are not called releases. No backup/restore semantics change.

All CI tools are copied (without starting the source container) from reviewed official image manifest digests, then rawbinarySHA256 checked: Restic0.19.1, legacy0.16.0, Syft1.57.0 (nativeamd64/arm64), Cosign3.1.4 (amd64 publicationrunner). The installer table is versioned in scripts/install-ci-tool.py; synthetic tests verify wronghash failure, cleanup and no source execution. This avoids repeated GitHubasset HTTP504 without disabling SPDX generation, provenance, exacttestedartifact binding or keyless signatures.

Restic registry binaries were independently matched to the checksum-verified official compressed release bytes. Cosign registry binary is a different official static build than its releaseasset; fixedmanifest+rawSHA+actual signing verification are required, no false byteidentity claim. No Alpine/sourceimage layers are shipped in our runtime.

Full all/lite nativeamd64/arm64 restore E2E and shared-base validation remain mandatory. Production requires per-project restore rehearsal, oldcommand migration, original repository and schedule preservation. Old MinIO snapshots are not deleted automatically.
