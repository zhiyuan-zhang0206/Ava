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

## Upgrade a legacy cluster

On the first gateway `ava start` after this version is installed, Ava brings up
the legacy bearer-backed data plane, applies migrations, then mints missing DB
owner, Redis admin, and Redis runtime passwords. It re-affirms the owner role,
changes Redis `requirepass`, re-creates the Redis ACL password, refreshes the
PgBouncer owner entry, writes the split values and URLs to the gateway `.env`,
and updates the running process before service sessions start.

If the split step is interrupted after Redis `requirepass` changes but before
the `.env` write, restart Redis so `redis-server` reloads its configuration and
restores the previous `requirepass`, then re-run `ava start`. The step is
idempotent and self-heals.

Fresh authenticated installs mint all three values at birth. Empty-bearer
single-box clusters are intentionally a no-op: all data-plane credentials remain
empty and local services stay unauthenticated.

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
