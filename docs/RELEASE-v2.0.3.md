# v2.0.3

Release orchestration fix: checkout source at the beginning of each publication job before downloading the tested image/evidence. A later clean checkout in v2.0.2 removed its validated identity/SBOM/provenance files after architecture images had been pushed, blocking signatures and indexes. Those partial architecture tags are not production releases; old tags are not moved.

No runtime backup/recovery semantics are changed. Adds a workflow regression test enforcing checkout-before-download and mandatory signing/evidence validation. All nativeamd64/arm64 all/lite E2E, SBOM and shared-base gates remain required, then exacttestedimage push and keylesssign. Production still waits for finished indexes and independent anonymouspull/signature verification.
