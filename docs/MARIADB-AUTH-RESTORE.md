# MariaDB authentication preservation during logical restore

## Scope and fixed defect

The previous MariaDB restore reused PostgreSQL's role-password helper. That
helper automatically adds the source connection username/password. MariaDB then
applied that one password to **every host entry for that username**, replacing
imported plugins/verifiers with mysql_native_password. A source root@localhost
and root@% (or an application user's two hosts) could therefore lose distinct
passwords, socket/PAM configuration and composite authentication.

The fixed MariaDB path does **not** call that helper. The PostgreSQL path and its
password provisioning semantics are unchanged: its native globals deliberately
omit role passwords, whereas MariaDB's native system-users export contains its
own authentication definitions.

## Default: preserve the source globals

With no new variable, MariaDB restore streams the original native globals and
database SQL into a new private target and does not generate ALTER USER for any
source account. The source connection credentials remain connection settings;
they do not request a credential reset and need not identify an account in the
snapshot. No new variable is required for existing production configurations.

Original user@host identity, authentication plugin(s), credential verifier and
grants remain owned by the native globals archive. The adapter does not rewrite
that archive or normalize unsupported plugins to a common password scheme.
Native import failure (including missing plugin/insufficient restore privileges)
is fatal. PAM, unix_socket and composite plugin metadata are **not** silently
replaced. Import success alone does not prove their external runtime dependencies
or a real application's authentication path works: those are separate gates.

The short-lived local bootstrap is a separate random account, never the source
connection account. Collision with a source username fails before starting the
helper. The bootstrap has the explicit PROXY grant required by native users
restore, is dropped on the successful path, and the helper must cleanly stop
before publication. Import/override/verification errors propagate and the helper
is stopped by its existing finally cleanup; no failed stage is published. A
failed stage can retain bootstrap metadata on disk, is private/unpublished, and
must never be started as an accepted application database.

## Optional exact-account override (explicit credential migration)

Do not configure this to obtain ordinary preservation. Use it only when a
separately reviewed credential change is intentional. Set
RESTORE_MARIADB_ACCOUNT_PASSWORDS_FILE to an absolute local file path. The file
must be a regular file, exact mode0600, owned by the process effective UID
(normally root), single hard link, no final symlink, at most1MiB, with this strict
schema. The containing directory should be root-only and the file must not be
placed in the backup stage or a source data volume. Do not put a real password
in a CLI argument, public manifest, repository file, log or this document.

Synthetic schema example only:

~~~json
{
  "schema_version": 1,
  "accounts": [
    {
      "user": "synthetic_app",
      "host": "localhost",
      "plugin": "mysql_native_password",
      "password": "synthetic-explicit-replacement-only"
    }
  ]
}
~~~

All four account fields are required. Unknown fields, role-only mappings,
duplicate JSON keys, repeated exact account identities, empty identities/passwords,
invalid Unicode/NUL, nonprivate files, missing files, unreadable files and FIFO or
symlink input fail closed. Only explicitly selected mysql_native_password
migration is implemented; other override plugins are rejected, not silently
coerced. To retain PAM/socket/composite authentication simply omit this mapping.
Anonymous-account overrides are deliberately unsupported.

Host is an **exact MariaDB account Host value**, not an instruction to enumerate
other host variants. An explicit host "%" changes only the account literally
stored as user@%; it never broadcasts over all accounts of that user. The
manifest must contain the precise account. After native import, the adapter
queries for exactly one case-sensitive user/host login account for each override
before changing any account. Missing or ambiguous actual accounts fail.

The selected account is explicitly changed to mysql_native_password using an
in-memory native verifier. The supplied plaintext is not included in SQL, argv,
logs or snapshot artifacts. The mapping itself remains an operator-controlled
private plaintext secret; the adapter reads it but does not copy or delete it.
Each override's plugin and authentication string are read back through an
aggregate boolean/count check. Native command/permission failure or a mismatch
is fatal. Grants are not deliberately edited by this path, but the exact
account's plugin change is a real credential migration, not preservation; verify
its grants and actual client access before cutover. If one of several overrides
fails, earlier changes may exist only in that failed private stage—there is no
claim of transactional account updates or successful publication.

### Old role-only option on MariaDB

RESTORE_ROLE_PASSWORDS_FILE remains supported by PostgreSQL as before. On
MariaDB, **the presence of that variable is an explicit error**, even if blank
or accompanied by the new option, because a username-only map cannot express
host-specific intent. It is not read, ignored or expanded over hosts. Migrate an
intentional old override to the exact user/host schema, or remove it to preserve
native globals. Existing deployments that never set either override variable
require no new setting.

Stable safe error categories include:

- ambiguous_mariadb_password_mapping: PostgreSQL role-only option supplied;
- invalid_mariadb_password_file / unsafe_mariadb_password_file: invalid or unsafe
  input (no password/path contents emitted);
- unsupported_mariadb_auth_override: requested override plugin unsupported;
- unknown_restore_account: missing exact snapshot/imported account;
- mariadb_auth_override_mismatch: requested final plugin/verifier not verified;
- database_command_failed / native process error: import/query/update denied or
  failed; no fallback credential rewrite.

## Synthetic verification and remaining gates

Unit tests execute no database servers, containers, production data, Secret reads
or network clients. They exercise same-name localhost/% accounts with different
native definitions; untouched socket/PAM SQL metadata; no implicit rewrite even
with source connection credentials; one-host and literal-percent-host overrides;
permissions/owner/mode/symlink/hardlink/FIFO/JSON errors; old role-only options;
actual-account absence, failed ALTER/verification and helper shutdown. Existing
PostgreSQL tests continue to run unchanged.

~~~sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -p test_engines.py -q
PYTHONDONTWRITEBYTECODE=1 python3 -B tests/container_e2e.py --self-test
~~~

The MariaDB container E2E **design** now creates same-user two-host credentials,
a root remote-host password different from the backup connection password,
unix_socket and distinct grants. It records SHOW CREATE USER / SHOW GRANTS only
in memory, backs up with real Restic/native dump, restores the exact snapshot
and compares the full synthetic account set/authentication/grants without
printing verifiers. A second empty target restores that same snapshot with one
explicit-host override, requires every other account unchanged, checks the
requested verifier and fixture schema grants, then cleanly stops. Existing
synthetic DB/schema/invariant checks and repository full check remain.

### Numeric final owner999 coverage

The harness also exposes case maria-final-uid999, included in the all case set.
Its source fixture runs as packaged mysql UID/GID998. Both the normal and explicit
single-host-override restores set DB_UID=999 and DB_GID=999. The test checks the
published root and every database entry is999:999, except the restore journal
which remains root-owned0600, and restarts the real MariaDB11.8 server as the
**numeric**999:999 identity using setpriv (not runuser mysql). It reuses the same
schema, authentication, grant and clean-shutdown assertions for both targets.

In this image UID999 resolves to the OS account postgres. That NSS name is
asserted before and after the case and is not edited or reassigned. Static
analysis shows the adapter already uses numeric setpriv for MariaDB install-db
and mariadbd when explicit DB_UID/GID are configured; the helper does not need
the numeric ID to be named mysql. This is **not native-execution proof** that all
MariaDB install script/NSS/runtime behavior works at999. Hosted CI must execute
this case on both image architectures; startup/install/ownership/auth failures
must fail the case rather than falling back to998 or chowning to another owner.

The old harness would falsely obstruct this test: it always restarted as mysql998
and used a private socket/tmp root owned998. The parameterized harness now gives
source and each restored instance a separate private0700 runtime directory owned
by that instance's numeric UID, under a root-owned traversal-only0711 synthetic
parent. No production path or source datadir is chowned. The mapping file stays
root-only0600 and the final999 NSS mapping is not changed.

Pure self-tests mock native process execution and verify numeric999 install and
server argv, ownership enforcement, unchanged default998 behavior, isolation and
CLI dispatch. They do not execute a database, container or Docker daemon. The
native case is ready for Lead's managed CI; its status in this task is NOT RUN.

This task does **not** run that container E2E, pull/build/push an image or interact
with production. Native execution status here is **NOT RUN** pending Lead's
separate reviewed-runtime/image authorization. No E2E pass is inferred from unit
mocks. PAM provider/configuration and a real application's network/identity,
PROXY/definer/roles, account TLS/limits, DDL consistency, full schema inventory,
RPO/write freeze and before-new-writes rollback remain project-specific cutover
gates. The preserved SQL cannot supply missing plugin libraries, PAM service
files, Unix peer identity or business permission proof.
