# PostgreSQL logical restore: helper UID and final owner

## Scope and current acceptance

This change applies only to PostgreSQL **logical** restore. MariaDB and native/legacy PostgreSQL physical paths retain their previous identity handling; do not claim this handoff has adapted them.

Local synthetic engine tests: **52 passed**, Python syntax checks passed. The hosted CI numeric-UID70 native database case and real source-specific Alpine continuation are **NOT RUN / pending Lead verification** at this handoff. No production rollout, Kubernetes mutation, container execution, commit or push was performed by this task. An older published image does not gain this feature without a new tested build.

## Why final DB_UID=70 cannot be the initdb execution identity

PostgreSQL initdb resolves its effective Unix UID through NSS even when --username specifies an explicit database bootstrap role. A numeric UID without an NSS passwd entry cannot run initdb successfully. The unified Debian image supplies a resolvable postgres helper account (currently 999:999), whereas an unchanged official Alpine database can run as 70:70.

Upstream evidence: [initdb get_id](https://github.com/postgres/postgres/blob/REL_17_STABLE/src/bin/initdb/initdb.c#L811-L828) calls get_user_name_or_exit, which uses [getpwuid(geteuid())](https://github.com/postgres/postgres/blob/REL_17_STABLE/src/common/username.c#L29-L53).

The logical adapter now separates:

- **Helper identity**: when the adapter runs as root, read the image's existing postgres passwd entry; require positive numeric UID/GID and a valid name. Do not create a UID70 passwd alias and never run initdb as root. A non-root adapter must resolve its current UID through NSS and uses its current primary GID.
- **Final identity**: DB_UID / DB_GID take precedence over POSTGRES_UID / POSTGRES_GID. Each omitted component defaults to the helper's corresponding ID. Values must be positive decimal integers below the uid_t -1 sentinel (4294967295), not zero/negative/arbitrary strings. The final numeric UID/GID does **not** require an NSS entry.
- A non-root adapter cannot request another final UID/GID. No privilege escalation is attempted.

Default configuration remains helper identity == final identity, so the ordinary 999 target does not receive the additional transfer walk. The existing passwd-based helper identity, not an env-supplied final UID, is used for initdb, temporary server startup, its Unix socket, signals and process-group shutdown.

## Order and safety conditions

1. Verify the already-restored snapshot manifest/artifact hashes and existing engine metadata. This change does not skip native decode, database identity, locale, extension, owner/ACL, password-verifier or subprocess return-code checks.
2. Pin the empty output target inode supplied to the logical restore callback. Run initdb/restore/validation entirely as the helper account. The input stage and target must not overlap.
3. Exit the temporary server context: clean fast shutdown and wait(returncode=0). Write final SCRAM HBA/configuration and fsync the created database tree.
4. If final differs from helper, require pg_controldata to report exactly "shut down" and reject any postmaster.pid/server.pid entry, even a stale/symlink marker. No transfer is attempted after an earlier restore/validation/shutdown failure.
5. Open the target component-by-component with dirfds and O_NOFOLLOW. Verify its recorded device/inode. Preflight the **entire tree before the first chown**: all entries must be real directories or regular files owned by the helper, with regular-file st_nlink=1, on the same mount ID/device. Reject symlinks, hardlinked regular files, FIFO/socket/device entries, unexpected owner, mount crossing and inode replacement. Depth is limited to 64 and total entries to 100000.
6. Transfer descendants before their directories, root last. Reopen every path relative to the pinned root using O_NOFOLLOW and recheck inode, owner, mode, links and mount. Use fchown, preserve the original mode (fchmod only if chown changed it), and fsync the opened inode. Never use shell chown -R, pathname symlink following or arbitrary deletion.
7. Rewalk the final tree, rejecting added/missing/replaced entries and owner/mode/mount deviations, fsync directories and reconfirm the target pathname still identifies the same pinned inode.
8. Return only after the transfer completes. The restore module then captures the final payload root metadata and publishes with its existing no-clobber journal/rename flow. This helper never changes journal/marker, runtime socket, input archive, source volume, target parent owner or unrelated tree. Temporary ancestor traversal permissions remain under the existing context's finally restoration.

The caller must continue to give this function only the newly-created, unpublished callback payload, not an existing live database. Existing nonempty source targets remain governed by restore-init's skip/no-clobber logic. A failed transfer may leave an unpublished private payload with mixed helper/final ownership; it does not record successful restore/publication. Existing restore journal identity and active-process gates govern retry/reset of that owned payload, never cleanup of arbitrary source data.

The ownership walk runs only with root permission for a differing target. It requires the normal minimal filesystem/process capabilities used by the restore helper, not privileged, host PID/network, a mounted kubeconfig or source PVC. Special set-id modes may require CAP_FOWNER to preserve exactly; failure remains fail-closed. Typical database directory0700/file0600 modes do not require an extra chmod after an unchanged mode.

## Compatibility that still must be demonstrated

A UID handoff is not proof of PostgreSQL ABI, locale, extension, configuration, image-entrypoint or application compatibility. For Blinko specifically, admission still requires the exact uploaded native snapshot to be restored by the new all image, then the unchanged official PostgreSQL17 Alpine digest to start that same isolated output as numeric70:70, pass metadata/object/extension/authentication checks and shut down cleanly. Same major, C/C.UTF-8 and null collation_version lower risk but do not replace this test.

A nested PGDATA such as /data/pgdata must already exist as an empty approved target for a fresh volume; restore-init does not synthesize arbitrary parent paths. Do not restore into /data if the main container expects /var/lib/postgresql/data/pgdata. The original source PVC, original major/image and business credentials remain untouched.

## Synthetic coverage

The engine suite exercises: helper NSS lookup vs arbitrary final IDs, missing/root helper accounts, aliases/precedence/ranges, non-root limits, no clean-shutdown or active-marker rejection, same-owner no-op, link/hardlink/special-file/mount/inode/foreign-owner rejection before chown, descriptor-pinned postorder transfer, mode/data/parent preservation, chown/fsync failures, full logical restore helper999→final70 sequencing, and failure/unclean-control suppression of handoff. A full-flow assertion binds the handoff's expected value to the actual target (device,inode), including nonempty extension metadata, preventing extension-loop variable shadowing from masquerading as a successful mock.

Unit command:

~~~sh
python3 -m unittest discover -s tests -p test_engines.py -q
python3 -m py_compile k3s_backup/engines.py tests/test_engines.py
~~~
