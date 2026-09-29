# Data-plane credential split

The cluster bearer and data-plane credentials have separate authority. The
internal data plane always authenticates, whatever the bearer
([decision](../decisions/2026-09-26-internal-data-plane-always-authenticated.md)).

| Authority | Holder | Purpose |
|---|---|---|
| `AVA_CLUSTER_SECRET` | Gateway only | Human/operator bearer for the gateway API and frontend login (never served by bootstrap, never held by a remote unit); empty = unauthenticated user-facing API and `/ops`, loopback-only listeners |
| `AVA_API_TOKEN` (launch environment; also `$AVA_HOME/run/ava-root/manifests.json`, 0600) | Each launched service, admitted operator processes | The write generation's machine API token of the process's class: the gateway admits the active generation's tokens, a unit's `/ops` its generation's two; delivered only while the API is authenticated. The manifest copy is root's own record of what it launched — it persists until the next start rewrites it, and is inert once a release fence revokes the generation |
| `$AVA_HOME/backups/logical-backup.passphrase` (0600) | Gateway home | The logical-backup passphrase: minted and pinned at birth (a home born earlier pinned `sha256(secret)` at its cutover), never derived and never changed by a secret rotation ([decision](../decisions/2026-09-28-backup-passphrase-minted-at-birth.md)); **backup-critical**: it is the only key to every logical backup |
| OS user over the owner-only socket (`peer`) | Gateway host | Postgres administrator: provisioning, migrations (acting as the NOLOGIN schema owner), grants, the authority fence |
| OS user mapped to `ava_monitor` (`peer map=ava_monitor`) | Gateway host's OTel collector | Password-less statistics reader (`pg_read_all_stats`, CONNECT); not a write generation, so no credential exists and rollouts leave it alone |
| `$AVA_HOME/db-authority/` (0700; files 0600) | Gateway home | `ledger.json` (owner, groups, active generation), `generations/<n>.json` (the write generation's two logins with passwords and SCRAM verifiers, and its two machine API tokens), `pooler-admin.json` (PgBouncer admin console `ava_pooler_admin`), `units/<key>.json` (each remote unit's enrollment secret) |
| `$AVA_HOME/db-authority/` (0700; files 0600) | Remote agent-runner home | `unit.json` (the installed runner login of one generation, bound to this unit and the served endpoint, with its API admission: the runner API token, the gateway token's digest and the telemetry token), `enrollment.json` (this unit's enrollment secret) |
| `AVA_REDIS_ADMIN_PASSWORD` | Gateway only | Redis `default` user and `requirepass` |
| `AVA_REDIS_PASSWORD` | Gateway file; embedded in `AVA_REDIS_URL` | Redis ACL runtime user |
| `AVA_RUNNER_DB_PASSWORD` | Remote-managed planes only | The provider-provisioned `ava_runner` login the gateway-local launcher projects for agents (never served by bootstrap) |

A local plane's `.env` carries only the credential-free database endpoint
(`postgresql://<owner>@host:port/<db>`). The schema owner is `NOLOGIN` without
a password. Every application login is a write generation: `ava_g<n>_gateway`
and `ava_g<n>_runner`, which inherit the `NOLOGIN` groups `ava_gateway` /
`ava_runner` and own nothing (`shared/cluster/authority/`).

Delivery:

- The root launcher puts each DB-using service's class login into that
  service's launch environment (`AVA_DB_URL` plus the non-secret
  `AVA_DB_GENERATION`); gateway-profile services get the gateway login, runner
  and agent processes the runner login. The launch digest binds the generation
  number and credential digest, never a password. The same login also lands at
  rest, 0600, in `$AVA_HOME/run/ava-root/manifests.json` — root's own launched-unit
  record, rewritten on the next start and inert after the next release fence.
- An operator process on the gateway home (the `ava` CLI, a script, an OS job)
  receives the gateway login only while it runs the home's admitted runtime:
  the selected release image, or the source checkout the home was born from.
  Anything else keeps the credential-free endpoint and its first dial fails
  with `NoDatabaseAuthorityError`. The one hand-over is the release handoff:
  the admitted CLI passes that login (no API token) in the exec environment to
  the executor image's submission, which is not selected yet; the finite
  executor it launches holds no login and dials as the OS-user administrator.
- Bootstrap serves no database credential: its `AVA_DB_URL` is the
  credential-free endpoint (a remote-managed provider URL loses its password),
  and a runner strips any password an older gateway still serves. A remote
  agent-runner's root launcher delivers its installed unit capability to every
  runner-class service, and an admitted operator process on that home consumes
  it; anything else refuses by name. The capability arrives only as an
  operator-issued bundle ([runbook](runbook.md)), so a stale runner cannot
  reacquire the current generation. Bootstrap never serves the human secret
  either, and the fetch authenticates with the unit's API token.

Agents never receive an admin password, `AVA_REDIS_PASSWORD` as a standalone
variable, or a gateway-class login. Agent-profile startup at the default home
rejects a loopback non-runner URL on a secret-bearing cluster.

## Convert an existing home

A home born before the data plane always authenticated has empty Redis
credentials, a LOGIN schema owner (with `AVA_DB_ADMIN_PASSWORD` on a secret
home), a LOGIN `ava_runner` (`AVA_RUNNER_DB_PASSWORD`), trust `pg_hba` lines and
no database authority ledger. `ava start` refuses it before any native effect
and names `scripts/cutover_db_authority.py`, the one explicit conversion;
nothing converts implicitly. Development and preview homes can be destroyed
and re-born instead. A networked home (remote agent-runners) additionally
rotates the human bearer once (step `api`) and classifies its remote units and
issues their capabilities (step `remote-units`) below; the runner-side cleanup
of retired keys, the human bearer included, belongs to the home adoption.

Run it from the checkout that owns the home (its `.venv`), in a gateway context,
with the application stopped (a networked home adds `--unit` / `--exclude-unit`
/ `--bundle-dir`):

```bash
ava stop --keep-infra
.venv/bin/python scripts/cutover_db_authority.py --home "$AVA_HOME"            # dry-run
.venv/bin/python scripts/cutover_db_authority.py --home "$AVA_HOME" --execute
ava start
```

A home in the fleet cutover is the exception: the home adoption
([cutover home adoption](cutover-home-adoption.md)) already stopped it under
the cutover hold, so skip `ava stop`, and never follow the conversion with
bare `ava start`. The script's last line names the next steps instead: the
[database records repair](cutover-db-records.md), then the held first start
`scripts/cutover_adopt_home.py --home "$AVA_HOME" --start`. While that hold
stands, an ordinary start refuses before the held first start and keeps the
hold after it.

`--home` must name the checkout's own home. The script refuses a remote-managed
plane, a home without a registry record, an active release operation, a running
application root and persistent terminals. The steps run in order:

- `redis` mints both passwords into `.env` (the runtime one also inside
  `AVA_REDIS_URL`), stops the owned password-less Redis under native custody
  with a final save, restarts it from a `redis.conf` carrying `requirepass`,
  re-affirms the ACL user with its password, and proves that an unauthenticated
  client is refused.
- `db` stops the owned pooler, rewrites the always-authenticated `pg_hba` and
  proves the running postmaster demands passwords, applies pending migrations,
  demotes the owner and `ava_runner` to `NOLOGIN` without passwords, creates
  `ava_gateway` and both groups' grants, proves every legacy session closed,
  creates the ledger, mints generation 0, restarts the pooler serving exactly
  that pair, proves both logins, activates the generation, checks the catalog
  invariant, and only then rewrites `.env` (credential-free `AVA_DB_URL`; the
  owner and runner passwords removed). A superuser owner is refused first.
- `api`: every home first pins its logical-backup passphrase to
  `$AVA_HOME/backups/logical-backup.passphrase`: `sha256(secret)`, what it has
  encrypted under so far, so every earlier logical backup keeps decrypting (an
  existing pin is kept; an empty secret pins a minted one, and its earlier
  artifacts restore only with
  `scripts/data_plane_ops/restore_drill.py --legacy-empty-secret-passphrase`). A single box
  keeps its secret. On a networked home every runner holds a copy of
  `AVA_CLUSTER_SECRET` and from now on authenticates with its generation's API
  token, so the secret rotates once (`scripts/rotate_cluster_secret.advance`,
  recorded in this journal as fingerprints only, pinning before it writes the
  new secret). The pinned file is backup-critical: verify the gateway's copy
  with its other backup keys before any runner copy of the old material is
  archived and removed. The telemetry relay token derives from the secret and
  changes here exactly once.
- `remote-units` reads `machine_units`: every unit other than this gateway unit
  must be classified exactly once, `--unit MACHINE:HOME` (included) or
  `--exclude-unit MACHINE:HOME` (paused or offline; it stays fenced); units of
  paused machines must be excluded. It rotates both Redis passwords, since
  runner homes, their archived residue and the pre-cutover backups hold copies
  of each: the admin one (applied with `CONFIG SET requirepass`, persisted to
  `redis.conf` and `.env`) and the runtime ACL one (re-affirmed on the ACL user,
  persisted to `.env` and `AVA_REDIS_URL`; runners fetch the new URL from
  bootstrap at their next start, which a held gateway still serves: the route
  is control-plane). Each is staged in
  `db-authority/redis-<admin|runtime>.pending` so a crash resumes with the same
  value, and each old password is proven refused. It then writes one sealed
  bundle per included unit into `--bundle-dir` (an
  owner-only directory), printing each transport key once; bundles issued after
  `api` carry the rotated telemetry token and the generation's API admission.
  A single box has no remote unit and the step is a no-op.

A home already born authenticated is only verified. Each step records its
intent before its effect in `$AVA_HOME/db-authority/cutover.json` (0600): an
interrupted run continues with what it already wrote (the same generation, the
same passwords), and a completed run repeats as a verified no-op. Ambiguous
state is refused before any change: partial Redis credentials, a Redis that
demands a password the home does not record, a ledger for another owner, or a
journal that contradicts `.env` or the ledger.

The rewritten `.env` changes the configuration digest, so a release request
prepared before the cutover must be prepared again.

Verify a converted home without printing credentials:

```bash
grep -E '^(AVA_DB_ADMIN_PASSWORD|AVA_RUNNER_DB_PASSWORD|AVA_REDIS_ADMIN_PASSWORD|AVA_REDIS_PASSWORD)=' "$AVA_HOME/.env" | cut -d= -f1
ava status
```

Only the two Redis key names may print; do not echo values, paste URLs into
tickets, or put passwords in command arguments.

## Routine data-plane rotation

PostgreSQL has nothing to rotate by hand: the owner never logs in, and
application logins rotate as write generations with each release transition.
`scripts/data_plane_ops/rotate_data_plane_secrets.py` rotates the Redis credentials only; they
do not rotate per rollout
([decision](../decisions/2026-09-27-write-generation-rollout-choices.md)).

Run it on the gateway checkout that owns the target cluster, in a gateway
process context (not an agent shell). It defaults to dry-run and has no
`--home` flag.

```bash
.venv/bin/python scripts/data_plane_ops/rotate_data_plane_secrets.py
.venv/bin/python scripts/data_plane_ops/rotate_data_plane_secrets.py --scope admin --execute
.venv/bin/python scripts/data_plane_ops/rotate_data_plane_secrets.py --scope runner --execute
```

`--scope admin` rotates the Redis `default` password (`requirepass`);
`--scope runner` rotates the Redis ACL runtime password; the default is both.
After runner scope, restart the gateway and every enrolled runner so each
reloads its Redis URL. Each execute writes a 0600 recovery file beneath
`$AVA_HOME/backups/secret-rotation/`; on failure, re-run the exact printed
`--execute --resume <state-file>` command rather than editing one side by hand.

## Emergency bearer rotation

`AVA_CLUSTER_SECRET` does not rotate with the data plane, and machine callers
never hold it. Rotate it only for a confirmed leak:

```bash
.venv/bin/python scripts/data_plane_ops/rotate_cluster_secret.py              # dry run
.venv/bin/python scripts/data_plane_ops/rotate_cluster_secret.py --execute
```

The script stages the next secret (`backups/secret-rotation/bearer.pending`),
journals the rotation as fingerprints (`backups/secret-rotation/bearer.json`),
verifies the pinned logical-backup passphrase (the secret never touches it;
only a home the cutover is converting pins `sha256(secret)` here), and only
then writes the new secret into the gateway `.env`; a re-run
resumes an interrupted rotation from its journal. Restart the gateway, then
issue every remote unit a new capability bundle: its telemetry relay token
derives from the secret. New browser logins use the new secret.
