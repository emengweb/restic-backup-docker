# v2.0.1

Packaging-only first-publication fix after v2.0.0 download infrastructure failures. The v2.0.0 tag is retained unchanged, not force-moved. All runtime adapters, automatic restore guards and schedules remain unchanged.

Restic0.19.1 is now copied from its official DockerHub multiarchitecture image pinned at index136600b6ff6843d61d355f7f71f460a166429f35de6fd11b568fece3c9a4d510; only /usr/bin/restic is copied, no Alpine layers. The amd64/arm64 binaries are ELF64 static Go (CGO0), verified raw SHA25620d4142678d0d95ec11a4759def1b73fd9190abc9ca19e4b62d067c0b387e639 /2fb45ac6f9071b6f20eb883953a188f9e7c7cb6bbe43c67a2e47ada4e85ee7f0. amd64 independently matches the original checksum-verified release.bz2 decompressed binary. arm64 is pinned to the verified official image/manifest/raw binary and still must pass exact version+native backup/restore CI; do not claim a failed HTTP504 download was checked.

Host modern Restic tests use the same fixed official amd64 binary without running the source container. Legacy Restic0.16.0 compatibility tests retain their original release checksum and a bounded download retry. Full all/lite nativeamd64/arm64 E2E, shared-base, provenance/SBOM and signing gates remain mandatory before any index release.

Production updates still require correct new command+args, data/work mounts, existing repository credentials and actual restore acceptance; this patch does not remove the v2.0.0 major-release rollout warnings.
