# Embedded per-cluster data plane

**Status: done, except the redis half of slice 3.** Slices 1 + 2 are implemented —
every cluster (including the prod default home) runs its own Postgres+Redis instance
under its `$AVA_HOME`; the shared-instance + logical-isolation model and
`cli/commands/_compose.py` are gone. Slice 3 (bundling the binaries so there is no
brew/apt dependency) is **half done**: Postgres is vendored
(`shared/runtime_binaries.py`), redis is not. That remaining leg lives entirely in
[`vendored-data-plane-binaries.md`](vendored-data-plane-binaries.md) — this doc does
not re-describe it.

This doc is kept as the **design record** — why physical per-cluster instances beat
hardening logical isolation, and what that deleted. The shipped model is described in
[`runbook.md`](../../conventions/runbook.md).

## The invariant

> **One cluster owns one Postgres instance and one Redis instance, both living
> entirely under that cluster's `$AVA_HOME`. Nothing in the data plane is shared
> between two clusters on a box.**

Cluster isolation becomes *home-directory isolation* and nothing else. Two clusters
on one machine cannot reach each other's data plane because there is no shared
instance to reach into — not because a database name, a Redis logical-DB index, a
channel prefix, and an ACL user happen to be configured correctly.

This is the philosophy.md thread applied to the data plane: **use the strongest
invariant that kills the ambiguity, rather than machinery to manage it.** The
shared instance + logical isolation is machinery managing the ambiguity "which
cluster does this row/channel belong to". Physical per-cluster instances make the
ambiguity structurally impossible.

## Why

Two clusters co-located on one box (the everyday dev case: several worktree
clusters; CI running suites concurrently) share one Postgres on `:5432` and one
Redis on `:6379`. Isolation is *logical*: `ava_<cluster>` database name + role,
Redis logical-DB index + `ava:<cluster>:*` channel prefix + ACL user, all kept
correct by configuration. When that configuration is even slightly wrong, clusters
write into each other's data — observed in dev — and tests that spin up ephemeral
data planes clash on the shared ports/instance (the documented "concurrent
ephemeral pg/redis instances clashing" flake, and the `test_claim` restart flake).

The cost of *getting the logical isolation right* has been a recurring tax on
bring-up code, on the per-cluster auth/provisioning model, and on test hygiene.
Physical isolation removes the class of bug instead of hardening the machinery
that prevents it.

The trade is favorable for Ava's scale: a per-cluster data plane costs ~100-150 MB
RAM (Postgres `shared_buffers` tuned down + Redis ~5 MB) — roughly *one agent's*
RSS. On any box already running agents it is noise; the expensive side of the
ledger is engineer-hours debugging cross-talk and writing isolation-aware tests,
which this removes. RAM is the cheap side; buying a machine or spreading clusters
across the machines on hand is simpler than engineering shared-instance isolation.

## What it deletes

The shared instance is gone, so every discriminator that existed to tell two
clusters apart *inside one instance* is unnecessary. The bulk is in
`cli/commands/_compose.py` and `shared/cluster/`:

- **Redis logical-DB index allocation + channel prefix** (`redis_db_index`,
  `redis_prefix`, `allocate_redis_index`, `redis_channel_prefix`,
  `ava:<cluster>:*`). Every instance is single-cluster: DB 0, bare channel names,
  no neighbour to prefix away from.
- **Shared-instance Redis ACL users + box-level admin-secret dance**
  (`redis_admin_secret`, `RedisAdminSecretMissing`). The physical-instance model
  keeps one runtime ACL user per cluster, but its `requirepass` is now the
  gateway-local Redis-admin credential and its runtime password is independent.
- **Per-cluster role inside a shared instance + the legacy-role reassignment**
  (`ensure_cluster_role`'s "reassign ownership from the legacy `ava` role" path,
  the bootstrap-superuser-provisions-every-cluster's-role model). Each instance
  `initdb`s its own superuser; a slim role + db owned by it uses the independent
  gateway-local DB-owner password for the scram TCP connection.
- **Shared-instance foreign/neighbour probes** — `_shared_infra_running`,
  `_foreign_redis_error`, `_redis_listening`. A per-cluster instance on its own
  port with its own data dir under `$AVA_HOME` is unambiguously this cluster's;
  "is the thing on :6379 ours, a neighbour's, or foreign" stops being a question.

## What stays: auth uniform, transport uniform

Transport is **TCP on a per-cluster port, single-box and multi-machine alike** —
no socket-vs-TCP branch. Keeping both topologies on the same transport is worth
more than the marginal isolation a unix socket would buy on a single box (it
would force single-box connection strings into socket paths while split clusters
stay `host:port` — an asymmetry not worth its weight).

`AVA_CLUSTER_SECRET` stays as the control-plane bearer. A per-cluster TCP port
on loopback is reachable by *any* local process — including a co-located
cluster — so each authenticated instance keeps independent data-plane locks:
the Postgres owner password, Redis `default`/`requirepass` password, runner DB
password, and Redis runtime ACL password. Isolation comes from *each cluster
having its own instance* (which kills cross-talk); authority is separated so a
runner cannot use its bearer to become an owner or Redis administrator.

Postgres bind posture is unchanged — loopback + this host's reachable address,
never all interfaces — while Redis is loopback-only; only the ports become
per-cluster instead of the fixed 5432/6379.

## Prod migrated (grandfathering removed)

This landed incrementally to protect prod data: slice 1 gave *new* clusters their own
per-cluster instance while prod stayed on the shared instance; a data-migration then
`pg_dump`/restored prod into its own instance (db `ava_main`, fixed pg 5433 / redis
6380 — off the default 5432/6379 so it never collides with a stray host pg/redis); slice
2 removed the grandfather branch and all shared-instance machinery. There is now one
model: every cluster owns its instance, and `uses_own_instance` is simply
`"postgres" in rec.ports` with no special case for any home.

## Implementation shape (re-scope, not rewrite)

At the time, `cli/commands/_compose.py` already managed native pg/redis lifecycle:
cold-start `initdb`, start/stop, config rendering. This change **re-scoped** that
management from per-host-shared to per-`$AVA_HOME` (it now lives in
`cli/commands/_cluster_instance.py`; `_compose.py` is gone). It was not a
from-scratch supervision lift.

- **Data dir moves under `$AVA_HOME`.** `_pg_data()` / `_redis_conf_*()` resolve
  to `$AVA_HOME/pg/` and `$AVA_HOME/redis/` instead of the brew/apt shared
  locations. Each cluster's postmaster and redis own that directory.
- **Postgres + Redis join the per-cluster `PORT_OFFSETS` block.** Every cluster's
  pg/redis gets an allocated port in its block (postgres = base+11, redis = base+12);
  the prod default home carries fixed pg 5433 / redis 6380, exactly as it keeps fixed
  gateway/health ports. Nothing binds the default 5432/6379.
- **`db_url` / `redis_url` point at the per-cluster TCP port** (loopback +
  reachable address, unchanged posture). The `DERIVED_ENV_KEYS` written at cluster
  birth carry the per-cluster port.
- **`initdb` template cache.** A fresh per-cluster `initdb` adds ~1-3 s to
  bring-up — noticeable in tests. Cache the `initdb` output as a template data
  dir and copy it per cluster, so spin-up is a directory copy, not a fresh init.
- **Birth no longer provisions a shared-instance ACL/redis-index.** For a new
  (non-prod) cluster it `initdb`s (or copies the template), starts the instance
  with the independent Redis-admin `requirepass`, creates the owner and runner
  roles, re-affirms the runtime ACL user, and runs the schema. The registry
  record drops `redis_db_index` / `redis_prefix`.

### Slices

The change splits along real seams. Grandfathering prod (above) forces the
ordering: the shared-isolation machinery cannot be deleted while prod still
runs on it, so deletion follows the prod migration, not slice 1.

1. **✅ Done — per-cluster instance path for new clusters.** A new (non-prod)
   cluster `initdb`s its own instance under `$AVA_HOME` on its allocated pg/redis
   ports, separate owner/Redis-admin/runtime credentials, runs the schema, and connects
   there; prod stayed on the shared path (byte-identical prod bring-up). `initdb`
   template cache so a new cluster / a test spins up by directory copy. Delivered the
   cross-talk fix + test-flake removal without touching prod.
2. **✅ Done — migrate prod off the shared instance + delete the shared machinery.**
   `pg_dump`/restore prod into its own per-cluster instance (pg 5433 / redis 6380),
   drop the grandfather branch, and remove the now-dead shared-isolation code
   (redis logical-DB index, the per-cluster `ava:<cluster>:*` channel-prefix isolation
   — channels are now the fixed `ava:*` — the box-level redis admin secret, the shared
   bring-up in `_compose.py`, and the foreign/neighbour probes) plus the vestigial
   `ClusterRecord` fields (`redis_db_index`, `redis_prefix`). **Kept**: the per-cluster
   Postgres role and redis ACL user (the runtime identities — carried by the cluster's URLs as data, now
   provisioned against the cluster's own single-tenant instance). Follow-up not taken
   here: dropping the redis ACL user entirely (runtime connects as `default` /
   requirepass), which would also retire the `redis-acl` watchdog healthcheck.
3. **Bundle the pg/redis binaries** — pg done, redis remaining. Orthogonal (a
   per-cluster instance runs the same whether the binary came from brew or a bundle),
   so it is tracked on its own in
   [`vendored-data-plane-binaries.md`](vendored-data-plane-binaries.md).

The "What it deletes" list above describes the end state, now reached in slices 1-2.

## Blast radius

- [`runbook.md`](../../conventions/runbook.md) — the entire shared-instance + logical-isolation
  description (per-cluster db/role/redis-index/channel-prefix, the `requirepass`
  vs ACL-user model, the bootstrap superuser) is rewritten to the per-cluster
  instance model with uniform per-cluster-port TCP + separately scoped credentials.
- Supersedes the north star in the `redis-data-plane-ownership` design note and
  the "external/managed data plane as a knob" framing — the data plane is always
  Ava-owned and per-cluster; the only remaining knob is whether an instance is
  reachable across machines (split deployment) or loopback-only (single box),
  which is just the existing bind posture, not a separate isolation model.
- Reverses much of PR #108 (redis admin-secret ownership) and PR #114
  (`AVA_PG_BOOTSTRAP_URL` / per-cluster role provisioning) — that machinery
  managed shared-instance isolation, which no longer exists. The external/managed
  Postgres knob (`AVA_PG_BOOTSTRAP_URL`) survives only if external-DB hosting
  remains a goal; otherwise it goes too.
- Connection budgeting — `max_connections` is now per-cluster, not
  a per-host shared 1000.
- Migrations are unaffected in shape (each cluster still runs the same schema);
  only *where* they run (per-cluster instance) changes.

## redis-bridge external-migration boundary (task #1945, WP3)

The `com.ava.redis-bridge` relay (`/usr/bin/python3
$AVA_HOME/redis-bridge/relay.py`, per host) is
the off-loopback Redis inbound mechanism: Redis always binds loopback-only
(see `docs/history/2026-08-24/redis-loopback-only.md`), and the bridge
forwards the host's private-network address + Redis port to `127.0.0.1`, so a
split deployment's runners reach the gateway's Redis over the tailnet without
Redis ever listening off-loopback. Facts registered here:

- **Station scenario (observatory on another machine)** — the bridge is
  independent of the observatory: Redis stays on the gateway host with the
  data plane; moving the LGTM backends (stage C) does not move Redis, so the
  bridge keeps its exact role (gateway-host relay for off-box consumers). The
  station's Grafana never dials Redis; its PG datasource comes from the
  data-plane URL (see `_observatory_urls._pg_datasource_host_port`), not the
  bridge.
- **Tailnet semantics** — the relay target is the host's reachable private
  (tailnet) address, so the bridge is what makes `redis://<tailnet-ip>:6380`
  work for runners; it is per-cluster-port and per-host, never a shared
  listener.
- **Completed 2026-09-05: repository ownership and recovery** — prod gateway
  converge installs the repository source and launchd desired state. The relay
  recreates a failed listener, while status and the periodic health probe issue
  an authenticated Redis PING through the bridge; process liveness alone is not
  treated as serving health.
- **Future external-migration direction** — when Redis is externalized (a
  SaaS or a foreign host named by the data-plane `redis_url`), the cluster
  treats the data plane as remote-managed: local bring-up, ACL provisioning,
  and the loopback bind are skipped, so the bridge is bypassed by the URL
  swap, not by changing the bridge. Its retirement is then per-host cleanup
  (stop the OS job on hosts whose clusters all name a foreign Redis). No
  bridge behavior changes for that migration; the migration surface is the URL.
