# Data-plane credential split

The cluster bearer and data-plane credentials have separate authority. The
internal data plane always authenticates, whatever the bearer
([decision](../decisions/2026-09-26-internal-data-plane-always-authenticated.md)).

| Authority | Holder | Purpose |
|---|---|---|
| `AVA_CLUSTER_SECRET` | Gateway and enrolled runners | Control-plane bearer for gateway API, `/ops`, bootstrap, and machine registration; empty = unauthenticated user-facing API, loopback-only listeners |
| OS user over the owner-only socket (`peer`) | Gateway host | Postgres administrator: provisioning, migrations (acting as the NOLOGIN schema owner), grants, the authority fence |
| OS user mapped to `ava_monitor` (`peer map=ava_monitor`) | Gateway host's OTel collector | Password-less statistics reader (`pg_read_all_stats`, CONNECT); not a write generation, so no credential exists and rollouts leave it alone |
| `$AVA_HOME/db-authority/` (0700; files 0600) | Gateway home only | `ledger.json` (owner, groups, active generation), `generations/<n>.json` (the write generation's two logins with passwords and SCRAM verifiers), `pooler-admin.json` (PgBouncer admin console `ava_pooler_admin`) |
| `AVA_REDIS_ADMIN_PASSWORD` | Gateway only | Redis `default` user and `requirepass` |
| `AVA_REDIS_PASSWORD` | Gateway file; embedded in `AVA_REDIS_URL` | Redis ACL runtime user |
| `AVA_RUNNER_DB_PASSWORD` | Remote-managed planes only | The provider-provisioned `ava_runner` login a remote plane's bootstrap projects |

A local plane's `.env` carries only the credential-free database endpoint
(`postgresql://<owner>@host:port/<db>`). The schema owner is `NOLOGIN` without
a password. Every application login is a write generation: `ava_g<n>_gateway`
and `ava_g<n>_runner`, which inherit the `NOLOGIN` groups `ava_gateway` /
`ava_runner` and own nothing (`shared/cluster/authority/`).

Delivery:

- The root launcher puts each DB-using service's class login into that
  service's launch environment only (`AVA_DB_URL` plus the non-secret
  `AVA_DB_GENERATION`); gateway-profile services get the gateway login, runner
  and agent processes the runner login. The launch digest binds the generation
  number and credential digest, never a password.
- An operator process on the gateway home (the `ava` CLI, a script, an OS job)
  receives the gateway login only while it runs the home's admitted runtime:
  the selected release image, or the source checkout the home was born from.
  Anything else keeps the credential-free endpoint and its first dial fails
  with `NoDatabaseAuthorityError`.
- Bootstrap projects the active runner login inside `AVA_DB_URL` for enrolled
  runners. This interim exchange lets a stale runner holding the bearer
  reacquire the current generation; per-unit delivery retires it.

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
and re-born instead. Networked homes (remote agent-runners) are not converted
by this script: their runners hold owner-era credentials and wait for the
fleet cutover.

Run it from the checkout that owns the home (its `.venv`), in a gateway context,
with the application stopped:

```bash
ava stop --keep-infra
.venv/bin/python scripts/cutover_db_authority.py --home "$AVA_HOME"            # dry-run
.venv/bin/python scripts/cutover_db_authority.py --home "$AVA_HOME" --execute
ava start
```

`--home` must name the checkout's own home. The script refuses a remote-managed
plane, a home without a registry record, an active release operation, a running
application root and persistent terminals. Two steps run in order:

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
`scripts/rotate_data_plane_secrets.py` rotates the Redis credentials only; they
do not rotate per rollout
([decision](../decisions/2026-09-27-write-generation-rollout-choices.md)).

Run it on the gateway checkout that owns the target cluster, in a gateway
process context (not an agent shell). It defaults to dry-run and has no
`--home` flag.

```bash
.venv/bin/python scripts/rotate_data_plane_secrets.py
.venv/bin/python scripts/rotate_data_plane_secrets.py --scope admin --execute
.venv/bin/python scripts/rotate_data_plane_secrets.py --scope runner --execute
```

`--scope admin` rotates the Redis `default` password (`requirepass`);
`--scope runner` rotates the Redis ACL runtime password; the default is both.
After runner scope, restart the gateway and every enrolled runner so each
reloads its Redis URL. Each execute writes a 0600 recovery file beneath
`$AVA_HOME/backups/secret-rotation/`; on failure, re-run the exact printed
`--execute --resume <state-file>` command rather than editing one side by hand.

## Emergency bearer rotation

`AVA_CLUSTER_SECRET` does not rotate with the data plane. Use it only for a
confirmed bearer leak:

```bash
.venv/bin/python scripts/rotate_cluster_secret.py
.venv/bin/python scripts/rotate_cluster_secret.py --execute
```

The script preflights the current bearer against `GET /api/bootstrap`, stages
only the new bearer in the gateway `.env`, and prints the enrolled-runner
checklist. Restart the gateway, distribute the new bearer out of band, then
restart every runner. If the gateway has not yet restarted, restoring the old
bearer in its `.env` is a safe cancellation. After restart, use the recovery
state to coordinate a deliberate rollback; never leave runners split between
bearer values.
