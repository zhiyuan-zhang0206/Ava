# Connection layer is swappable — URL-switched local ↔ remote/SaaS data plane

## Context

Task #1752 (user ruling 2026-08-28 18:06: a cluster needs only internet reachability and key-authenticated mutual access; build the architectural structure first, service redistribution later)
asks for the architectural structure of a **replaceable connection layer**: a
cluster's Postgres + Redis must be able to live on this gateway box, on another
machine of the private network, or with a SaaS provider — and switching between
those must be a config change (URL + credentials + a data migration), not a
code change.

The data-dial side was already URL-first: every business process dials
`AVA_DB_URL` / `AVA_REDIS_URL` as-is (`shared.db.connect` / `pool`,
`shared/redis_client.py`), the runner data plane already lives off-box
(tailnet PgBouncer 6433), the observability-station PG data source is
decoupled, and the `pg_dump` restore path is drill-verified. What was missing
is the **management plane**: bring-up (`ava start` → `ensure_cluster_instance`),
teardown (`ava stop` / `ava cluster down`), health checks (`ava status`, the
watchdog's `redis-acl` / `pgbouncer` probes), role/ACL provisioning, credential
rotation, and the converge-time URL rewrite all assume a *local* instance and
would mis-manage (or try to repair) a foreign service when pointed at one.

## Decision

### The URL is the switch — one predicate, `settings.data_plane.is_remote`

A data plane is **remote-managed** iff either URL's dial host, after the
existing self-dial loopback rewrite, is a foreign (non-loopback) host:

- a local self-built instance keeps loopback URLs (a self-named host is
  rewritten to loopback by `_loopback_if_self`);
- an off-box host on the private network (the `AVA_DATA_PLANE_HOST` birth
  knob, `ClusterRecord.data_plane_host`) names another machine;
- a SaaS URL names the provider's host.

No new knob: the URLs *are* the switch, so "change the URL = switch" holds literally. A
mixed plane (one URL loopback, one foreign) also reads as remote — local
management is skipped wholesale rather than half-applied. The unanchored boot
sentinel and host-less (unix-socket) URLs read as local, so the predicate never
misfires on the pre-install or admin-socket paths.

### What remote-managed changes (the management plane degrades)

| Operation | Local (unchanged) | Remote |
|---|---|---|
| `ava start` data-plane step | `ensure_cluster_instance` (initdb / pg / redis / PgBouncer / ACL) | skip local bring-up; probe both URLs (auth-included connect / PING); unreachable → fail fast with the dial detail and the migration hint |
| `ava stop` / `ava cluster down` | `stop_cluster_instance` tears down pg/redis/pooler | no-op: "remote-managed — nothing to stop locally" |
| `ava status` data-plane section | `pg_isready` + pooled connect + admin redis PING + local pooler line | probe the URLs with their own credentials; no local pooler line; "remote-managed" marker |
| watchdog roster (gateway) | `redis-acl` + `pgbouncer` repairs | both skipped (no local ACL user / pooler to repair) |
| credential split (`_data_plane_admin_secrets`) | journaled local transition | no-op (provider owns credentials) |
| runner-grant refresh after migration | local `GRANT` refresh | skipped with a note (provider provisions roles) |
| `scripts/rotate_data_plane_secrets.py` | local ALTER ROLE / ACL / pooler userlist | fail fast: rotate at the provider, update the URLs here |
| `ava cluster ensure-db-role` | local role provisioning | fail fast: provisioned at the provider |
| converge pooler step (`_ensure_pgbouncer_step`) | rewrites AVA_DB_URL port to the pooler listener | skipped: the URL's port is the provider's, never this cluster's |

Business code is untouched in every row: `shared.db.connect` / `pool` and the
redis clients dial the URLs before and after the switch, so the only
requirements for a switch are the URL + credentials in `.env` and the data
migration (`pg_dump` / restore — drill-verified).

### Connection parameters into config (TLS / pool / ACL mapping)

- **TLS (Postgres)**: `AVA_DB_SSLMODE` (default empty). Empty keeps the URL and
  libpq in charge ('prefer' — today's behavior; a SaaS URL that already carries
  `sslmode=require` / `verify-full` is respected **as-is**: the URL is the
  switch, the field is a fallback default, never an override). Set
  `require` / `verify-full` to force TLS on a remote plane whose URL is silent;
  `disable` is the deliberate plaintext escape hatch for a trusted private
  network. Injected only at the sanctioned entry points
  (`shared.db.connect` / `pool`) and only when the URL is silent, so a stricter
  URL mode can never be downgraded.
- **TLS (Redis)**: rides the URL — `rediss://` scheme + `ssl_ca_certs` etc.
  query params, as redis-py documents. Nothing to add.
- **Pool sizing**: `AVA_DB_POOL_MIN_SIZE` / `AVA_DB_POOL_MAX_SIZE` (defaults
  1 / 2 — the historical literals) become the defaults of
  `shared.db.pool()`; an explicit per-caller size always wins. A managed data
  plane with tight connection limits tunes its pool footprint from config.
- **ACL mapping**: names-as-data, already in place — the URL userinfo *is* the
  identity (owner role / `ava_runner` / Redis ACL user). Remote-managed simply
  stops the local provisioning that used to mint those identities; the mapping
  itself never moves into code. The owner-password re-derivation
  (`_apply_data_plane_passwords`) is likewise exempted from FOREIGN URLs: a
  provider/SaaS credential is authoritative and must never be replaced by the
  local owner password — without that exemption, the switch path silently
  broke on every secret-bearing cluster (QA #867 P1). The self-dial loopback
  rewrite runs first, so a local instance named by its own reachable address
  keeps the self-heal; only a genuinely foreign host is exempt.

### Startup / health-check posture for an unreachable remote

`ava start` fails fast with an actionable message naming the URL host and the
dial detail (the gateway cannot serve without its data plane, and the boot
policy retries exactly as it does for a dead local instance). `ava status`
reports per-component `✓` / `✗` against the URLs and never touches local
machinery. The watchdog stops repairing what it does not own. There is no
silent "degraded" mode: either the URLs answer or the operator sees exactly
which one does not.

## Implementation surface

- `shared/config/data_plane.py`: `db_sslmode` / `db_pool_min_size` /
  `db_pool_max_size` fields + the `is_remote` property.
- `shared/db.py`: `_sslmode_for_url` (URL-silence check) + `_resolved_pool_size`
  (config defaults) applied in `connect()` / `pool()`.
- `cli/commands/_cluster_instance.py`: `remote_pg_reachable` /
  `remote_redis_reachable` probes; `stop_cluster_instance` and
  `print_data_plane_status` remote branches.
- `cli/commands/start.py`: `_ensure_gateway_data_plane` remote branch (probe,
  skip bring-up, fail fast); runner-grant refresh skip.
- `cli/commands/_data_plane_admin_secrets.py`, `_pgbouncer.py`,
  `ensure_db_role.py`, `scripts/rotate_data_plane_secrets.py`,
  `services/watchdog/daemon.py`: remote guards per the table above.

## Alternatives rejected

- **A dedicated `AVA_DATA_PLANE_REMOTE` boolean**: rejected — the URL already
  carries the fact (a non-loopback dial host), and a separate flag could
  disagree with the URLs it describes (flag says remote, URL says loopback).
  Deriving the mode from the URLs keeps "the URL is the switch" literally true.
- **Remote default `sslmode=require`**: rejected — the existing remote-host
  topology (tailnet PgBouncer, plaintext over the private network) is a remote
  data plane that speaks no TLS; a remote→require default would break it. The
  field defaults to empty and only an operator-set value (or the URL's own)
  changes TLS.
- **Config field overriding the URL's sslmode**: rejected — psycopg merges
  kwargs over URL conninfo params, so injecting would silently downgrade a
  stricter URL mode; the URL stays the primary source.

## Follow-ups (out of scope for the structure)

- Install-time remote birth (a fresh `install.sh` whose data plane is remote
  from day one) — the switch path documented here is an *existing* cluster
  changing its URLs; a remote-born cluster's schema provisioning is the
  service-redistribution phase the user deferred.
- Agent-process pool sizing (`agent/loop.py`'s own pool) stays code-level;
  SaaS connection-limit tuning for agents is a config-field follow-up if a
  concrete provider budget demands it.
- SaaS candidate evaluation (cost / migration / compliance) remains a
  user-decision item under #1752 once the structure is in place.
