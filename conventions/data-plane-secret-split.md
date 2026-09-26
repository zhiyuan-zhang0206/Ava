# Data-plane credential split

The cluster bearer and data-plane credentials have separate authority.

| Authority | Holder | Purpose |
|---|---|---|
| `AVA_CLUSTER_SECRET` | Gateway and enrolled runners | Control-plane bearer for gateway API, `/ops`, bootstrap, and machine registration |
| `AVA_DB_ADMIN_PASSWORD` | Gateway only | Main Postgres owner role and PgBouncer main entry |
| `AVA_REDIS_ADMIN_PASSWORD` | Gateway only | Redis `default` user and `requirepass` |
| `AVA_RUNNER_DB_PASSWORD` | Gateway file; runner URL projection | Least-privilege `ava_runner` Postgres role |
| `AVA_REDIS_PASSWORD` | Gateway file; embedded in `AVA_REDIS_URL` | Redis ACL runtime user |

Agents must never receive either admin password, `AVA_REDIS_PASSWORD` as a
standalone variable, or a main-identity `AVA_DB_URL`. Bootstrap always projects
the `ava_runner` URL; its Redis URL carries the runtime ACL password.

The same rule applies to every `AVA_PROCESS_PROFILE=agent` process, not only
detached agents. In particular, the single-box hosted agent-host receives an
explicit `ava_runner` URL from both the initial service launch and its watchdog
respawn. Agent-profile startup rejects a loopback owner URL on a
secret-bearing cluster: profile hygiene intentionally removes owner credentials,
so deriving an owner password from `AVA_CLUSTER_SECRET` there would create an
invalid mixed credential instead of a legal runner connection.

Redis always authenticates, whatever the bearer: first start mints
`AVA_REDIS_ADMIN_PASSWORD` and `AVA_REDIS_PASSWORD` for every local data plane,
including an empty-bearer single box, and they do not rotate per rollout
([decision](../decisions/2026-09-26-internal-data-plane-always-authenticated.md)).
Postgres credentials still follow the bearer: an empty-bearer single box keeps
its owner password empty and Postgres unauthenticated on loopback.

## Convert an existing home

A home born before Redis always authenticated has empty Redis credentials and a
Redis without `requirepass`. `ava start` refuses it and names
`scripts/cutover_db_authority.py`, the one explicit conversion; nothing converts
implicitly. Development and preview homes can be destroyed and re-born instead.

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
application root and persistent terminals. The `redis` step mints both passwords
into `.env` (the runtime one also inside `AVA_REDIS_URL`), stops the owned
password-less Redis under native custody with a final save, restarts it from a
`redis.conf` carrying `requirepass`, re-affirms the ACL user with its password,
and proves that an unauthenticated client is refused. A home already born
authenticated is only verified. Each step records its intent before its effect in
`$AVA_HOME/db-authority/cutover.json` (0600): an interrupted run continues with
the credentials it already wrote, and a completed run repeats as a verified
no-op. Ambiguous state is refused before any change: partial Redis credentials,
a URL whose password is not `AVA_REDIS_PASSWORD`, a Redis that demands a
password the home does not record, credentials no journal claims while Redis
still serves unauthenticated, or a journal that contradicts `.env`.

The rewritten `.env` changes the configuration digest, so a release request
prepared before the cutover must be prepared again.

Verify a completed split on the gateway without printing credentials:

```bash
grep -E '^(AVA_DB_ADMIN_PASSWORD|AVA_REDIS_ADMIN_PASSWORD|AVA_REDIS_PASSWORD)=' "$AVA_HOME/.env" | cut -d= -f1
ava status
```

The expected key names are printed by the first command; do not echo values,
paste URLs into tickets, or put passwords in command arguments.

## Routine data-plane rotation

Run this only on the gateway checkout that owns the target cluster. It defaults
to dry-run and has no `--home` flag.

Run it in a gateway process context, not from an agent shell. An agent context
sees the runner-projected `AVA_DB_URL`, while its profile hygiene removes the
admin data-plane password variables; preflight would therefore misidentify the
owner role. The script fails closed with an invocation-posture error instead.
This is a footgun guard against accidental agent-context invocation, not an
authorization boundary.

```bash
cd <gateway checkout (e.g. ~/.ava/source)> && unset AVA_PROCESS_PROFILE && set -a; . ~/.ava/.env; set +a && .venv/bin/python scripts/rotate_data_plane_secrets.py ...
```

```bash
.venv/bin/python scripts/rotate_data_plane_secrets.py
.venv/bin/python scripts/rotate_data_plane_secrets.py --scope admin --execute
.venv/bin/python scripts/rotate_data_plane_secrets.py --scope runner --execute
```

`--scope admin` rotates the owner Postgres password and Redis `default`
password, then refreshes PgBouncer's main user entry. `--scope runner` rotates
both runner credentials, replays the Redis ACL, and refreshes PgBouncer's runner
entry. The default scope is both.

Runner scope requires a restart of every enrolled runner after success. The
PgBouncer reload updates new database connections but cannot replace an already
cached runner URL; restarting makes the runner fetch the current projection.

Gateway-local agent and agent-host launchers project the runner database URL
from one fresh gateway `.env` snapshot: both the endpoint/database and runner
password in that projection belong to that snapshot. Neither a cached owner
URL nor an old runner URL is combined with a newly read password. A missing
snapshot URL fails before launch instead of falling back to boot-time Settings.
Enrolled remote runners are different: their authenticated bootstrap URL is
authoritative and passes through unchanged; they never consult a local gateway
runner password or synthesize a runner identity from an owner URL. The bootstrap
response and local launcher share the same pure credential projection function.
Bootstrap likewise requires its database URL from that same file snapshot;
it cannot substitute a cached Settings URL when the file omits it. Noncredential
defaults retain the existing Settings resolution. Connection pools bind the
complete configuration captured at process startup and are closed at shutdown;
configuration changes take effect through the existing restart boundary, not
a hot pool swap or a new credential-generation protocol.
This is per-call consistency, not a credential rotation protocol or proof that
the database's live verifier has already been changed. Connection credentials
must not be included in logs or manifests.

The gateway process must also restart after any data-plane rotation. Its
in-memory connection URLs and admin passwords remain stale until it reloads
`.env`, so new DB connections and Redis reconnects fail; for runner scope, its
in-memory `AVA_REDIS_URL` retains the old runtime password.

Each execute writes a 0600 recovery file beneath
`$AVA_HOME/backups/secret-rotation/`. On failure, re-run the exact printed
`--execute --resume <state-file>` command. Do not attempt a manual rollback by
changing only a URL or only one server password: the saved state is the authority
for completing the idempotent phases. If the recovery file is unavailable, stop
and reconcile the actual Postgres, Redis default, Redis ACL, and PgBouncer states
before changing the gateway `.env`.

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
