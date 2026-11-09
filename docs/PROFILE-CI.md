# Profile image CI and publication

## Status and ownership

This is the local implementation of the final two-image release contract: **only the default all-in-one image and an opt-in lite image without database capability**. No database-specific or database-major-specific image tags are built or published. It has not been built, run, pushed or exercised on GitHub during this edit. Static checks are not container E2E evidence. Publishing remains disabled unless the repository owner explicitly enables it.

The implementation is in the [publication workflow](<../.github/workflows/docker-publish.yml>). The Lead owns Dockerfile targets and the container E2E harness; the runtime collector has a separate owner.

## Profiles, defaults and tags

Both profiles use the same GHCR repository. Each gets its own native `linux/amd64` and `linux/arm64` image, followed by a same-profile multi-architecture index. No index mixes profiles.

| Profile / harness argument | Docker target | Version index example | Source-commit index example |
| --- | --- | --- | --- |
| `all` — default all-in-one | `all` | `v1.2.3-all`, **`v1.2.3`** | `sha-<40-character-commit>-all`, **`sha-<40-character-commit>`** |
| `lite` — opt-in, restic/files only, no DB tools | `lite` | `v1.2.3-lite` | `sha-<40-character-commit>-lite` |

The source Git release tag is `vX.Y.Z` (numeric components), optionally followed by a Docker-tag-safe prerelease suffix such as `-rc.1` or `-beta.2`. Build metadata (`+...`) is not accepted. CI derives profile suffixes; users do not create separate profile-suffixed source tags. Architecture tags add `-amd64` or `-arm64` after the profile, for example `v1.2.3-lite-arm64`.

Only `all` publishes the unsuffixed/default version and source-commit aliases, and CI requires their digest to equal the explicit `-all` index digest. Lite never occupies an unsuffixed/default tag. Moving aliases are disabled by default. Only when the owner explicitly sets `UPDATE_MOVING_ALIASES=true`, a stable numeric `vX.Y.Z` release updates `latest` to the signed all-in-one index and `lite` to the signed lite index. Prereleases such as `-rc` or `-beta` never update either moving alias. `docker pull ghcr.io/<owner>/<repo>` means `:latest` and is always intended to select all-in-one, never lite. If the extra gate has not been enabled for a successful stable release, an existing latest remains unchanged (or may be absent); this local implementation does not claim that an untagged pull already selects the new image. Internal compatibility targets, if any, are not additional release profiles. Only `all` and `lite` can enter the build, tested-artifact, architecture-publication or index matrices.

The index digest is printed in the job summary as `ghcr.io/<owner>/<repo>@sha256:...`. Production consumers should pin that digest after acceptance. Image tagging does not change the runner's automatic-latest-snapshot selection.

## Read-only test path

Pull requests, configured branch pushes and manual dispatches execute the same test matrix without package-write or OIDC permissions. PRs use `pull_request`, never `pull_request_target`. Checkout does not persist credentials. No GHCR login, production secrets, kubeconfig, production snapshots or real environment files are used by test jobs.

1. `prepare` resolves one source commit and reads its commit timestamp. Every image job checks out that exact commit. The timestamp is passed both as `SOURCE_DATE_EPOCH` and a BuildKit build argument.
2. `unit` installs checksum-pinned restic binaries, runs synthetic unit and local repository integration tests, and rejects empty or skipped test suites. The ownership suite runs with sudo on an ephemeral hosted runner and an environment allowlist containing only PATH/HOME/LANG and the two synthetic restic binary paths; this is necessary for actual chown tests, not a production privilege grant. No GitHub/cloud/production credentials are forwarded to that process. It also checks the harness self-test and shell syntax.
3. `image-test` expands only `all` and `lite` across native Ubuntu AMD64 and ARM64 runners: four builds and four profile-specific E2E runs. Missing ARM64 capacity does not permit QEMU or an AMD64-only release. Runtime metadata must establish the reviewed runc floor; otherwise testing fails rather than reporting success.
4. Each image is built once with its named target by the pinned Buildx, with a real `--metadata-file`, and loaded into the local daemon without pushing. Build provenance is read from that file by the tested extractor; large metadata is never expanded into an environment variable or shell argument. The E2E command is:

   ```sh
   python3 tests/container_e2e.py --runtime-reviewed --runtime runc \
     --docker-host unix:///var/run/docker.sock --image "$IMAGE" --profile "$PROFILE"
   ```

   `all` must execute the complete supported files/PG16/PG17/MariaDB matrix. `lite` aliases file cases and must not invoke database cases. Removing dedicated database image tags does not remove any database test case from `all`. The harness must fail, not silently skip or fall back to host binaries, when a required tool/case is unavailable.
5. CI records the tested image ID, native architecture, profile, target, source commit, epoch and layer diff IDs, plus real BuildKit max provenance, SPDX SBOM and E2E output. All are hashed. Release-tag runs also archive the exact tested image with `docker save`; later publication never rebuilds it. Artifacts are named by both profile and architecture and retained for three days.

## Shared base is a checked invariant, not a naming convention

The all-in-one target must inherit the real common files base. `lite` is the published alias for that files-only image; `all` adds the complete database runtime delta. Separate jobs must not copy/rebuild a superficially identical common root filesystem using the build wall clock.

The workflow uses one source epoch across all four image jobs and the Docker exporter option `rewrite-timestamp=true`, so layer archive timestamps as well as `image.created` are normalized. It rejects an image whose creation timestamp differs from the selected source epoch. Pinned sources, package inputs and deterministic collector output remain Dockerfile/collector requirements; an epoch by itself cannot make nondeterministic file contents identical.

Two independent gates enforce sharing within each architecture:

- Before any publishing, `shared-base` compares the two tested images for that architecture. The complete lite image's ordered `RootFS.Layers` diff-ID list must be an identical prefix of the all-in-one image. The source commit, epoch, target/profile and architecture must also match. Any difference blocks publication.
- Before publishing multi-architecture indexes, CI compares the actual compressed registry layer digests from all four published architecture manifests. For each architecture, the lite image's layer list must be an identical prefix of the all-in-one image. This verifies registry/host layer deduplication, not merely equal unpacked contents.

AMD64 and ARM64 naturally have different base blobs; no cross-architecture byte identity is claimed. Native architecture tags can already exist if a later index/signing/sharing check fails. A failing job does not roll back or delete pushed artifacts; the release must be treated as incomplete.

## Publication authority and artifact binding

Publication requires all test and shared-base jobs to succeed, a trusted `push` of a validated version tag, repository variable `ENABLE_GHCR_PUBLISH=true`, and entry into `ghcr-release`. The owner must configure that environment with tag/deployment restrictions and required reviewers before enabling the variable. Environment protections and immutable/protected Git release tags are repository administration, not settings this YAML can enforce. Manual dispatch and PRs cannot publish.

Write permissions are scoped only to publication jobs: `packages: write` and `id-token: write`; `contents` remains read-only. Login uses the short-lived job `GITHUB_TOKEN`, never a long-lived registry password or production credential. Actions, BuildKit, Buildx, cosign and Syft are pinned. Download/hash/load/identity checks precede GHCR login; write authority is not exposed to a checked-out source script.

For each profile/architecture, the publish job:

1. Downloads only the same-run, exact profile/architecture artifact and verifies its checksums and identity metadata.
2. Loads its saved image and checks image ID, OS, architecture and complete diff-ID sequence against the tested identity.
3. Tags and pushes that image ID under version and full-source-SHA tags, without build or E2E rerun.
4. Resolves the pushed manifest digest, confirms its config digest equals the tested image ID and records its actual registry layer digests.
5. Signs that digest and attaches the tested image's SBOM and the actual captured build-provenance predicate with keyless cosign/OIDC. No reconstructed predicate is substituted when build metadata is missing.

Each profile's index job requires all four signed architecture receipts and shared-registry-base verification, selects only its own AMD64 and ARM64 digests, creates version and full-SHA indexes, checks that both tags have the same digest and signs the resulting index. Only the all-in-one index job also publishes unsuffixed version and SHA aliases, with the same two architecture digests and the same resulting index digest. After signing, stable releases may additionally update the `latest`/`lite` aliases through the separate `UPDATE_MOVING_ALIASES=true` owner gate; the channel digest must equal the already-signed profile index digest. The index job is serialized per profile across workflow runs to prevent simultaneous alias writes, but it does not impose semantic-version ordering: the owner must not enable moving aliases when replaying an older stable release. Architecture SBOM/provenance attestations remain bound to their individual architecture digests. Consumers must verify expected repository/workflow issuer and certificate identity, not accept an arbitrary valid signature.

## Honest limitations and acceptance checklist

- Local static checks prove syntax and selected invariants only. Native build/E2E, reproducibility, registry operations, attestations and signatures are NOT RUN until the workflow actually succeeds.
- `SOURCE_DATE_EPOCH` plus timestamp rewriting is necessary but not sufficient for reproducibility. The diff-ID and registry-digest gates deliberately fail when any shared-base content differs.
- Hosted runner availability and reviewed runtime version are hard test prerequisites. The workflow does not lower these gates for convenience.
- Saved artifacts expire after three days. An expired or missing artifact cannot be replaced by a release-job rebuild; repeat the full trusted build/test workflow.
- The `ENABLE_GHCR_PUBLISH` and `UPDATE_MOVING_ALIASES` variables default to disabled when unset. Enabling either is a separate owner action. No local publication or production deployment is authorized by editing this workflow.
- Acceptance requires four profile/native-architecture E2E successes (with the full database suite still passing in each native all-in-one image), both shared-base checks, two signed profile indexes, correct all-in-one default aliases, stable-only optional `latest`/`lite` channels, per-architecture SBOM/provenance, and recorded immutable digests. A unit-only green check is insufficient.
