# Runtime model

## Clusters, units, prod, and dev clone paths

A **cluster** = one logical deployment. Every cluster — including `main` — owns its
OWN Postgres + Redis instance (under its `$AVA_HOME`, on the fixed pg/redis
ports), so a cluster shares no data plane at all: isolation is
home-directory isolation, not a database name / redis logical-DB index / channel
prefix kept correct inside one shared instance. A cluster also owns one outward
gateway on the fixed port table. (The rationale — and the remaining slice 3,
bundling the pg/redis binaries — is in
`future/infra/embedded-per-cluster-data-plane.md`.) A data plane is **swappable**:
URLs naming a foreign host (another machine or a SaaS provider) make the cluster
treat it as remote-managed — `ava start` / `ava stop` / `ava status` / the root health loop
skip local instance management and degrade to reachability probes, and the
connection-layer knobs (TLS, pool sizing) live in config
(`docs/history/2026-08-28/connection-layer-swappable.md`).
**One DB URL.** Every normal process configures exactly one database URL
(`AVA_DB_URL`) and dials it as-is — its port is chosen at URL generation
(install birth / converge, by `AVA_PGBOUNCER_ENABLED`): the cluster's PgBouncer
listener when pooling is on (the default; 6433 on the default home), the direct
Postgres port when off (5433). There is no separate pooler-port env key
(`AVA_PGBOUNCER_PORT` is retired): the pooler port is a registry-record fact for
the data-plane bring-up alone. The admin plane — migrations, `pg_dump`,
provisioning — is the ONLY direct-Postgres consumer. On a locally owned
plane it dials the home's owner-only socket as the OS user, acting as the
schema owner for schema work and dumps (`base.db.pg_admin`), never
the owner's own login or a write generation; a remote-managed plane uses its
provider URL (`base.db.direct_db_url`). Everything else dials `AVA_DB_URL`
as-is. Flipping `AVA_PGBOUNCER_ENABLED=false` is the kill-switch:
converge rewrites the URL to the direct port on the next `ava start` and the
pooler never starts.

Fresh install creates LangGraph's checkpoint schema with
`PostgresSaver.setup()` acting as the cluster owner. Start never calls setup: after
applying Ava's tracked SQL files, every capability reads the complete
`checkpoint_migrations` set and requires the explicitly approved version.
Checkpoint readers and agent boot therefore need CRUD but no schema CREATE.

`AVA_CHECKPOINT_INTERVAL` defaults to `4`, which writes every fourth super-step
(about 75% fewer checkpoint writes before terminal flushes). A crash can replay
up to three super-steps (re-spending LLM tokens and possibly replaying tool side
effects); claimed/pending reconciliation re-delivers inbounds, and checkpoint
parent chains span four steps. A thread using a `DeltaChannel` is exempt: the
throttle retires for it, every super-step persists as upstream wrote it, and a
crash on such a thread replays at most the in-flight super-step. Set a
per-agent `{"checkpoint_interval": 1}`
config overlay plus an agent restart, or set `AVA_CHECKPOINT_INTERVAL=1` in the
cluster `.env`, to restore every-super-step persistence. The full recovery
verification and rollback protocol lives in
[`docs/conventions/checkpoint-interval-canary.md`](../docs/conventions/checkpoint-interval-canary.md).

An upstream dependency bump that adds checkpoint migration version N must ship
that DDL as an Ava timestamp migration and advance
`CHECKPOINT_SCHEMA_AVA_MIGRATIONS` (the upstream baseline stays frozen at 9).
The migration must be idempotent when fresh
install setup already created both its schema effects and its
`checkpoint_migrations` row, while still letting Ava record its own migration
name. Real-Postgres tests must cover both existing-N-1 update and fresh-N
birth -> first-start registration. Until all of that ships together, the
dependency-drift gate fails before any database mutation, preserving the
ability to recover an interrupted update; a mistaken migration is fixed
forward (there are no down migrations).

**Identity is the home path** — there is no cluster name; the display label is
the home's basename. A cluster's database and the Postgres role that owns it
share one identifier, carried by its `.env` connection URLs **as data**
(`base.cluster.identity_from_url`): a fresh birth writes the fixed `ava`;
prod stays on its historical `ava_main` until an ops rename edits the URLs.
The role is `NOLOGIN NOSUPERUSER` without a password, owning only its own
database; that instance's own `initdb` superuser provisions it over the private
owner-only unix socket (`peer`), acting as the owner for every schema object.
No application process ever logs in as it.

A **unit** is one install of Ava under its own `$AVA_HOME`, and `AVA_HOME`
locates the unit's `.env`, logs, memory pool, milvus data, pidfiles, etc., all of
which derive from it. The home is `AVA_HOME` when set, else `~/.ava`
(`base/host/env/dotenv_boot.py:resolve_ava_home` — see "How a unit finds its home"
below), read whenever it is needed:

A machine carries a **capability set** — `gateway`, `agent-runner`, or both:

- **gateway** capability: owns the HTTP gateway + the data plane (Postgres /
  Redis / Milvus) + the gateway daemons for its cluster.
- **agent-runner** capability: hosts agents and the ops server, using agent-host
  through agent-host scheduling; its
  DB/Redis/Milvus URLs point at a gateway node when the host carries no
  `gateway` capability of its own.

A **single-box** deployment carries gateway and runner capabilities in one
home. A split deployment initializes a gateway with `ava init --serve-gateway
--no-serve-agent-runner` and joins each runner through `ava init
--serve-agent-runner --no-serve-gateway --gateway-url URL` with its machine
identity and environment bearer. Separate units use separate homes.

**Cluster identity is bound by `ava init`.** The settings-free entry validates
home and capabilities, then durably records credentials and the port table and
publishes configuration. It starts nothing and creates no resource; an interrupted
init resumes with `ava init` and no flags, and an initialized home refuses a second
one. The first `ava start` then converges host prerequisites, starts owned PG/Redis,
prepares the database and checkpoints, applies migrations and runner grants, starts
PgBouncer, then waits for every selected root service to become ready. A failure
preserves the same initialization intent; a retry does not rotate credentials or
select new ports. Between `ava init` and the first start the home's `.env` can be
edited (provider keys, for one) without a restart.

The home is `AVA_HOME`, else `~/.ava`, never the current directory. A bare
repeated start keeps identity and desired service selection, and takes no identity
flag. A home `ava init` has not initialized, unknown existing resources, missing
reservations, and a terminal destroy intent refuse rather than reconstructing
ownership.
A home describes only itself: its record is its own start intent, and no host
file lists clusters; the state a host shares (vendored runtime, initdb template,
PTY freeze, coding-session owners) lives in the home too. See
[[cli/docs/start_identity.ava.okf.md]].

A runner fetches the gateway's authenticated bootstrap configuration before
`ava init` records local identity. Bootstrap serves no database login: `AVA_DB_URL` is the
credential-free endpoint. The runner's login arrives in a capability bundle the
gateway operator issues for that one unit and the runner installs at `ava init` (and, for
a later bundle, with `install-unit`):

```bash
# on the gateway (its checkout's CLI), for one unit:
ava cluster db-authority issue-unit --machine <name> --home <unit $AVA_HOME> --out <bundle>
# carry the 0600 bundle to the unit and the printed transport key separately; then, on the
# unit (its checkout's `.venv/bin/ava` — the host's bare `ava` is linked by its first start):
read -rs AVA_DB_CAPABILITY_KEY && export AVA_DB_CAPABILITY_KEY
.venv/bin/ava init --db-capability <bundle>         # first join, plus the identity flags
ava cluster db-authority install-unit <bundle>      # a later bundle: stop the unit first, start it after
```

The bundle is sealed (AES-256-GCM) under a transport key printed once; it names
one unit (machine + home), the endpoint bootstrap serves, the active write
generation and an expiry (`--ttl-hours`, default 24, at most 72). Start refuses
an altered bundle, the wrong key, another unit's bundle, an expired one, an
older generation than the installed one, and a login the cluster rejects (a
revoked generation); it then writes `$AVA_HOME/db-authority/unit.json` (0600)
and deletes the bundle. A runner without a capability refuses to start and names
the issue command. Issue is refused on a remote-managed plane. A new generation
reaches remote units only by a new bundle (join, emergency).

**Guard a bundle like the generation it carries.** Nothing in it is the unit's
own. The runner login and runner API token are the write generation's, shared by
every runner unit, and the telemetry token is the cluster's until the human
secret rotates. A bundle with its transport key therefore gives its holder every
runner unit's database and API admission for that generation (bootstrap, with its
Redis runtime URL and provider keys, and every unit's `/ops` included). Its
machine binding only stops an install on the wrong unit by mistake: the
installer asserts its own machine name, and the credentials work without
installing. When a unit is compromised or a bundle and its key are lost, rotate
the write generation and issue every unit a new bundle; rotate the human secret
(telemetry token), the Redis runtime password and the provider keys as well, by
hand, in the order of
[manual rotation after a credential leak](#manual-rotation-after-a-credential-leak).
Detail: [[base/cluster/authority/docs/unit-bundle.ava.okf.md]].

Its DB/Redis connection facts are not cached locally: every runner process
fetches them at Settings construction. Start the gateway first, then the
runners. Gateway unavailability fails runner startup; the boot policy retries
the same start entry.

**Application services belong to one root per home.** On macOS, root descends
from the signed permissions helper; on Linux, root runs directly or under
systemd. Service identity and readiness are bound to that root's captured native
generation. Persistent PTY shells and native PG/Redis have separate custody.

**CI is a separate hosting surface.** The workflows provision isolated native
test infrastructure; see [CI](#ci-continuous-integration). Neither tests nor
build tooling may target a production cluster home. Container assets elsewhere
in the repository do not establish a cluster runtime dependency.

**Ports** come from one fixed table (`base/host/env/port_table.py`: gateway 8000,
frontend 3000, pg 5433, redis 6380, pgbouncer 6433, daemon healthz ports in
8103-8116, milvus 19530). A new home records the table in its start intent at birth, and
every later read is `rec.ports[...]` off that record; a unit whose `.env` names no
port binds the same numbers. Health probe URLs + daemon/milvus/frontend ports
derive from settings. The table is closed: a record with more or fewer slots is
refused at start. Tests never use these numbers (`base/cluster/tests/test_fixed_ports.py`).

prod runtime and dev workspace are split at the filesystem level:

| Path | Role | Notes |
|---|---|---|
| `$AVA_HOME/source/` (default `~/.ava/source/`) | **prod** — cwd of the long-running service sessions | git working tree; upgrades go through `python -m cli.fleet_update` ([Updating a networked cluster in source mode](#updating-a-networked-cluster-in-source-mode); `ava.self.update()` was removed 2026-08) |
| `~/Ava/` | **dev clone** — root of worktree-driven development; dev worktrees live under `.worktrees/<task>/` (made by `scripts/setup-worktree.sh <task>`) or `.claude/worktrees/<task>/` (Claude Code's native worktree tool; complete it with `scripts/setup-worktree.sh` inside it) | freely checkout any branch, decoupled from prod |

### Worktree uv iron rule (Tasks #1572, #5638)

An editable install is a pointer stored in the **active virtualenv**, not a fact
derived from the shell's current directory. A worktree's `.venv` must be a
real directory inside that worktree, never a symlink. Before **every** worktree
sync, run the dependency-free preflight; it refuses an external environment,
a symlinked `.venv`, or editable records naming another checkout. Discard an
inherited `VIRTUAL_ENV` first — for the preflight too, since the guard refuses a
leaked environment before it checks anything else:

```bash
env -u VIRTUAL_ENV python scripts/host_ops/guard_editable_venv.py .
env -u VIRTUAL_ENV uv sync
env -u VIRTUAL_ENV uv pip install -e .
```

Every worktree `uv` invocation must carry `env -u VIRTUAL_ENV`, including
`uv run`, `uv sync`, and `uv pip`. Never run bare `uv pip install`: an inherited
`VIRTUAL_ENV` can target the shared production environment and remove its
launcher while another service is using it (incident #4629).

On PowerShell, apply the same rule with `Remove-Item Env:VIRTUAL_ENV
-ErrorAction SilentlyContinue` before `uv`. Never rely on `cd` alone to select
the worktree's `.venv`. `scripts/setup-worktree.sh` invokes the same preflight
before its dependency synchronization.

Before deleting a worktree, inspect every long-lived Ava virtualenv on the host:

```bash
find "$HOME/Ava/.venv" "$HOME/.ava/source/.venv" \
  -name _editable_impl_ava.pth -print -exec sed -n '1p' {} \;
```

Each printed target must be its stable checkout root (`~/Ava` for the dev clone,
the installed prod source for prod), never the worktree being removed. The same
check applies to the editable URL uv records beside the pointer — in each venv,
`cat` the `ava-*.dist-info/direct_url.json` and confirm `url` is the stable
checkout's `file://` URL. If either record is wrong, do **not** delete the
worktree: use [manual editable-install recovery](#manual-editable-install-recovery)
from the affected stable checkout and recheck. `ava converge` / `ava start`
independently assert and auto-repair both prod records. Converge no longer
marks site-packages, `ava-*.dist-info`, or `.venv/bin` read-only itself; a
directory still carrying an earlier converge's `0o555` opens automatically
inside the repair's own write window
(`base/deploy/release/editable_install.py:protected_editable_paths` /
`editable_pth_write_window`), or by a one-time manual `chmod u+w` on that
directory — after either, `uv sync` behaves normally again.
Every `execute_code` spawn also checks
the current interpreter's records: the first poisoned call repairs the install
and returns a retryable structured error, preventing a flood of failed child
imports. The dev-clone pointer remains part of this mandatory deletion check.
This is the operating half of the editable-install guard specification; the
incident and escape analysis are in
[`postmortems/0006`](../postmortems/0006-an-editable-install-is-a-cross-checkout-pointer.md).

### Manual editable-install recovery

Editable installation repair is a development-checkout operation; see
[Editable Install Guard](../cli/commands/docs/editable-install-guard.ava.okf.md).

A typical small deployment runs the **gateway as a single-box unit** on an
always-on host (`gateway,agent-runner`, one home `~/.ava`, code `~/.ava/source`,
`main`'s own PG/Redis instance running **natively** via `pg_ctl` + `redis-server`
under `~/.ava` on ports 5433/6380, no docker). Any other
agent-runners stay single-home `~/.ava` and reach that gateway + DB/Redis over
the private network. A larger deployment splits the gateway onto its own
gateway-only host (the explicit `--role gateway` install).

**How a unit finds its home** (`base/host/env/dotenv_boot.py:resolve_ava_home`, read
every time the home is asked for, never captured at import): `AVA_HOME` when the
variable is set, else `~/.ava`. That is the whole rule — no pointer file, no
checkout claim, no in-process override — so a process and the children it spawns
cannot disagree about their home. Production does not depend on the variable. A
process tree that must not touch the host's cluster sets it once, at its top, and
every descendant inherits it:

- the test session (`tests/fixtures/env_bootstrap.py`) sets a temporary home before
  anything imports application code;
- every script a git hook launches that reaches application code calls
  `dotenv_boot.enter_scratch_home()` before its first import that does, behind
  `if __name__ == "__main__":` (tests import these modules, and a pytest process
  refuses the call): a fresh temporary `AVA_HOME` and `AVA_CONFIG_FETCH=skip`, whatever
  the caller's environment carries. Hooks run on every commit and push by nobody's
  choice, which is why this one is code
  (`scripts/tests/test_hooks_scratch_home.py` derives the scripts from
  `.pre-commit-config.yaml`);
- every other script that imports application code is run on purpose, and follows the
  convention in `conventions/dev-setup.md`: a temporary `AVA_HOME` in a development
  checkout. `scripts/check_worktree_remove.py` is the one tool that must read the real
  home: it reads this machine's live session records (`$AVA_HOME/run/pty`), so it only
  skips the gateway config fetch, dials nothing and writes nothing; run it straight from
  a checkout.

**Which checkout may operate a home.** A home that carries its own `<home>/source`
checkout (the production home `~/.ava`; every unit started from source) is operated
only by that checkout's code (`dotenv_boot.home_checkout_error`). The CLI gate
(`cli.preflight.require_own_checkout`, the first thing `cli.main` does,
settings-free) refuses every command when the running CLI belongs to any other
checkout, `status`, `ls` and `get` included; the only passes are bare `ava` and a
lone `-h`/`--help`, which parse and run no verb. First start and the service
launchers apply the same rule. A home with no `source` — a test or scratch home —
accepts any checkout. The way out of the refusal is in its message: run
`<home>/source/.venv/bin/ava` (the bare `ava` of a production host), or name a home
of your own with `AVA_HOME`. `python -m cli.fleet_update` already drives every host
through `$HOME/.ava/source/.venv/bin/ava`, so it is never refused.

Without the variable a development checkout resolves to the host's own cluster: on a
development machine that also runs production, that is production. Its CLI does not
touch it, not even to read; read production with the host's bare `ava`.

`.env` lives at `$AVA_HOME/.env`; each co-located unit carries its own. A dev
worktree with `AVA_HOME` unset resolves to `~/.ava` like any other process: tests
and tools therefore name a home of their own (above), and a real database is
reached only through a home that was started.

`.env` is the single config source of truth (precedence `env > Field default`; no
override layer). To add/change a value — a cluster secret like an API key, or a host
field — use `ava config set KEY=VALUE` (keyed by env-var or field name;
`--machine NAME` targets a remote agent-runner's host fields), or the Control page.
Both write the right `.env` (cluster → the gateway's, host → the machine's own) and
report which processes to restart for it to take effect — they never restart anything
themselves. `ava config get [KEY]` / `ava config unset KEY` round it out. Cluster
values reach agent-runners + agents by bootstrap on their next restart; a rotated key
needs only an agent restart, not a gateway restart. Which bucket a field falls
in is declared on the field itself — every `Settings` field carries a `scope`
(`cluster-pinned` / `cluster-default` / `host` / `agent`) in `base/config/`,
and `BOOTSTRAP_FIELDS` is derived from it.

Every official `.env` write is audited in `$AVA_HOME/.env.audit.jsonl` (0600): site, actor
(`user_session:<subject>` / `cluster_bearer:<subject>` / `cli:<os-user>`), `trace_id` when the
write arrived over HTTP, key names, and old/new values for non-sensitive fields only (record v2,
task #3588). Read the trail with `ava config audit [--last N] [--key K] [--machine <name|all>]`
or `GET /api/config/audit?machine=<name|all>&last=N` (merged newest-first; `all` fans out to
every agent-runner plus the gateway's own box). An out-of-band edit is detected at the next
`GET /api/config`: the guard appends a
self-rate-limited `unauthorized` record and emits an `env_unauthorized_write` anomaly. Rehearsal:
hand-edit `.env`, read the config once, then confirm the anomaly in the event stream and the
rebuilt audit line.

Postgres and Redis run as native processes (no Docker — the binaries come from brew's
`redis@8.2` keg on macOS / apt on Linux, but Ava drives them directly via `pg_ctl` + `redis-server`,
not `brew services`/launchd/systemd). Every cluster — including `main` — brings up its
OWN pair under `$AVA_HOME` on its per-cluster ports (`cli/commands/data_plane/cluster_instance.py`):
`initdb` into `$AVA_HOME/pg` (template-cached through a host-level dir beside the
registry, so a new cluster / a test spins up by directory copy rather than a fresh
multi-second init), plus `redis-server` with its data dir under `$AVA_HOME/redis`.
`ava start` ensures this cluster's instance is up and `ava stop` tears it down — there
is no standalone infra verb, and no shared host instance to survive across
checkouts/worktrees.

To select an already-installed Redis build for one unit, run that checkout's
CLI on its owning host:

```bash
.venv/bin/ava config set --local redis_bin_dir=/absolute/redis/bin
.venv/bin/ava config get --local redis_bin_dir
```

This persists `AVA_REDIS_BIN_DIR` only in the target home's `.env`. The directory
must be absolute and contain executable `redis-server` and `redis-cli`; both
start and probe/stop commands use that pair. A missing or nonexecutable tool
fails clearly, without falling back to PATH. An inherited selection is replaced
by the current home's declaration, or cleared when that home has none. Empty or
`ava config unset --local redis_bin_dir` restores the existing brew/PATH default.
No host PATH, package, preview, or Docker configuration changes are required.

The setting does not replace a live Redis process. `ava start` retains an
already-running instance. Change the running version only through an explicitly
coordinated data-plane stop/start, after checking persistence and version
compatibility. Keep the selected directory available across boot and updates;
the config selects tools but neither downloads nor upgrades them.

The data-plane posture is uniform — the default is multi-machine, a single box is just
the case where the reachable address is loopback (no single-vs-multi branch). The internal
data plane always authenticates, whatever the bearer
([details](data-plane-secret-split.md)). Postgres `pg_hba` admits the OS user only by
`peer` on the owner-only socket (the administrator, and through the `pg_ident` map the
password-less monitoring role `ava_monitor` the collector scrapes as) and every other role
by SCRAM; PgBouncer
is always `auth_type = scram-sha-256` against a userlist holding exactly the active write
generation's two SCRAM verifiers plus the admin-console entry `ava_pooler_admin`, and a
changed userlist restarts the pooler (a reload keeps a removed user that already
authenticated). Redis `requirepass` is the gateway-only `AVA_REDIS_ADMIN_PASSWORD`; the ACL
runtime identity uses `AVA_REDIS_PASSWORD` embedded in `AVA_REDIS_URL`. The bearer decides
only reach: an EMPTY bearer — the single-box default — serves the user-facing API
unauthenticated and binds every data-plane listener to loopback. `.env` holds only the
credential-free database endpoint; the root launcher delivers each DB-using service its
class login (gateway or runner) from `$AVA_HOME/db-authority/`, and an admitted operator
CLI receives the gateway login (see the secret-split page). A home born before this is
refused by `ava start` before any native effect; no conversion exists
([details](data-plane-secret-split.md#homes-born-before-this-model)). Settings never
rewrites a database credential. On the same load, a data-plane URL whose host is this machine's own
reachable address (`AVA_MACHINE_HOST`) dials `127.0.0.1` instead
(`base/config/data_plane.py`): self-dial never leaves the box. The `.env` value,
bootstrap payload, and registered address stay untouched, so remote runners keep dialing
the gateway's real address.

**Direct `psql` access.** The administrator: `psql "host=/tmp/ava-pg-<home-slug>
port=<pg port> dbname=<db>"` as the OS user (peer; add `options='-c role=<owner>'` to act
as the schema owner). An application role reproduction uses the active generation's
login from `$AVA_HOME/db-authority/generations/<n>.json` (0600; `<n>` is
`ledger.json`'s `active.number`) — read it, never paste it into tickets or argv.

**Capability groups and write generations** (Task #1236, `base/cluster/authority/`):
application privileges live on two `NOLOGIN` groups, never on a login. `ava_gateway`
holds DML on every table, `USAGE, SELECT, UPDATE` on sequences, `EXECUTE` on every
routine and PostgreSQL 17 `MAINTAIN` on the checkpoint tables (the blob vacuum fails
instead of silently skipping without it). `ava_runner` — the historical runner login,
demoted in place — holds exactly the audited runner surface: SELECT on every table (plus
sequence USAGE), SELECT/UPDATE on `agents_meta` (status/liveness), SELECT/UPDATE/INSERT on
`inbound_messages` (claim AND the agent-side self-lifecycle inbounds), UPDATE on
`agents` (`ava.self.set_label`), INSERT/UPDATE/SELECT on `machine_units` + INSERT/UPDATE
on `machines` and `host_deploy_state`, INSERT/UPDATE/DELETE on `api_idempotency`,
INSERT/UPDATE on `agent_tasks`, UPDATE on `agent_pages`, the shell
TTL rows, and full CRUD on the LangGraph checkpoint tables.
`agents` INSERT, `agents_meta` INSERT, notices writes, the cluster deploy-state tables and
any DDL fail under it by construction. Each write generation is one `ava_g<n>_gateway` and
one `ava_g<n>_runner` login inheriting its group (`INHERIT TRUE, SET FALSE, ADMIN
FALSE`); generation 0 is minted at birth. The point-in-time `ALL`
grants are re-run by every gateway `ava start` after migrations (`ensure_groups`), and
standing `ALTER DEFAULT PRIVILEGES FOR ROLE <owner>` covers objects later migrations
create; start then sweeps every non-active application login to `NOLOGIN` and holds on
any catalog/ledger mismatch (`check_invariant`).

The Redis ACL user comes from `AVA_REDIS_URL` independently of the Postgres
db/role in `AVA_DB_URL` (for example, Redis `ava` and Postgres `ava_main`).
Startup and credential splitting preserve both names. The redis ACL user is added
live at `ava start` (`ensure_cluster_redis_acl`), scoped to
the cluster's pub/sub channels (`ava:*`); it is re-affirmed on every start (not persisted
to redis.conf) and by the `redis-acl` gateway-watchdog healthcheck, so a redis restart
that drops the in-memory ACL is repaired before agents reconnect. Provisioning uses that
instance's own `default` user (the independent Redis admin password). The ACL user always
carries its runtime password; no identity is ever created `nopass`. A `.env` whose
redis_url carries no username has no ACL identity: `redis_identity()` raises
`ValueError`, so startup and the healthcheck fail instead of guessing one.
**Postgres and PgBouncer bind loopback + this
host's reachable address (`AVA_MACHINE_HOST`, default `localhost`), de-duplicated**
(never all interfaces): a single box resolves to loopback alone, while a split node
sets its real private-network IP, which is appended, plus the `scram-sha-256`
`AVA_TRUSTED_CIDRS` pg_hba ranges. Authenticated Linux Redis uses that same
loopback + reachable-address bind directly, without a relay. macOS retains its
loopback-only Redis workaround: the host-level `com.ava.redis-bridge` relay
(`/usr/bin/python3 $AVA_HOME/redis-bridge/relay.py`) forwards the host's private-
network address and Redis port to `127.0.0.1`; the prod gateway's converge step
installs that script from the repository-owned `services/redis_bridge/relay.py`
and owns the launchd plist. If the private-network interface or listening
descriptor fails, the still-running relay closes and recreates the listener with
capped backoff. `ava status` and the periodic cluster health probe issue an
authenticated Redis `PING` through this endpoint, so a running launchd PID with a
dead listener is reported rather than certified. No-secret Redis remains
loopback-only on both platforms. Each per-cluster pg is started with
`max_connections = 500` (each agent process holds ~4 steady conns), passed on the
`pg_ctl start` line; pg_hba is written into `$AVA_HOME/pg/pg_hba.conf` and —
when the server is already running — reloaded (SIGHUP) so the rewritten hba takes
effect immediately instead of at the next restart. First-start identity and
configuration are durable before PG starts. Repeated `ava start` it skips the bring-up when this cluster's pg/redis are already up
(`pg_isready` + a redis PING), and on a fresh start Postgres (and PgBouncer) first
waits (bounded, ~60s) for the reachable bind address to appear on an interface — so
a reboot that starts `ava` before the private-network interface exists retries rather
than dying on an un-bindable address. Authenticated Linux Redis uses the same
bounded wait; macOS Redis and no-secret Redis need only loopback. pg/redis are never touched when already up, so a (re)start never
disrupts a running data plane — with ONE deliberate exception: a running
pgbouncer that answers on loopback but is missing its reachable-address listener
(a silently degraded double bind, task #1288) is RESTARTED rather than reloaded,
because a SIGHUP reload never
retries a `listen_addr` that failed to bind at startup. `ava stop` tears this cluster's
own instance down (data persists on disk).

`repo` here is the checkout the running `ava` belongs to (resolved from where its `cli` source
lives, `cli/commands/_repo.py:_repo_root`), **not** the current directory — so a given `ava` always
targets the same cluster no matter where you run it. Invoke the checkout's
`.venv/bin/ava` for first start: `ava` on PATH does not exist yet. On the production
home its converge phase links `~/.local/bin/ava` to the checkout's `.venv/bin/ava` on every
source `ava start` and applies the rest of the host wiring. That global `ava` is the
production CLI, acting on `AVA_HOME`, else `~/.ava`. For dev, run `.venv/bin/ava` inside the
worktree with `AVA_HOME` set to a temporary directory (a worktree's CLI is refused on the
production home).

The converge phase (`cli/commands/converge/host.py:converge_host`) is idempotent — run
by every source `cmd_start`. Run it standalone with `ava converge`. It covers the
prod `ava` link, `~/.local/bin` on PATH, the `$AVA_HOME` dir skeleton, and one prod-host integration for
external agents: when `~/.codex` and/or `~/.claude` already exists, it copies only
`.agents/skills/operating-ava-cluster` into that client's global `skills/` root. Missing
client homes are not created. A private per-client ledger under `$AVA_HOME/configs/`
binds the installed generation to its in-target marker and a digest of names, kinds,
bytes, and modes. The ledger also records each generation's expected path manifest,
so interrupted staging and partially completed cleanup remain named and safely
resumable. Write-ahead phases precede stage publication and target claim, then
reconcile their no-replace outcome from both paths plus marker, digest, and
manifest evidence; ambiguous generation-shaped paths remain fail-closed.
A per-target process lock serializes claim-and-verify updates; claims and
restores are atomic no-replace renames. Cleanup records the residue and each
file's source, claiming, and quarantine state before its no-replace rename into
the private ledger root. Because supported filesystems provide no portable
identity-bound unlink, verified residue is terminally retained there and is
not retried, path-unlinked, or chmodded; the active client target remains
unblocked. Multi-link files are rejected. Unmanaged or changed copies and
unsafe linked paths are preserved with a client-labelled warning.
External-client failures do not abort core converge. Dev worktrees skip this
host-global step. Converge also applies a gateway-host guard that fails
loud on frontend build-time env overrides (`ui/web/.env{,.local,.production,.production.local}`
bake `NEXT_PUBLIC_*` into the bundle and silently beat the runtime gateway inference —
the 2026-06-09 outage), plugin config images, and the pre-rename disabled-services marker
carry-over (below). Converge never runs plugin scaffolds or touches the memory pool;
explicit `ava memory init` brings up the memory checkouts and seeds `MEMORY.md` plus the
commit-cap hook. The unit-state plugin-image step needs a configured unit, so on a
brand-new host it first runs during `ava start`.
On a gateway host, the selected root service roster includes **Gate**, the fleet
UI entry. It owns the public `frontend` port slot and proxies the Next.js app on
the separate `app` slot. Root starts and stops it with the application tree;
planned downtime includes the entry listener. Its dedicated `/__ava/healthz`
protocol plus captured root listener ownership provide readiness independently of
gateway/app availability. See [Fleet UI Gate](../services/gate/docs/gate.ava.okf.md).
**A rollout does not update built-in schedule scripts.** The `schedules` table is
authoritative and boot-time provisioning only inserts rows that are missing, so a changed
template in `schedules/` reaches a running cluster only through an explicit
`ava schedules update <name> --script-file <template>` (which relaunches an enabled
schedule on the spot) — see [`schedules/README.md`](../schedules/README.md).
Persistent `ava-schedule-<id>` terminals survive pause and update with their
currently loaded runner code and script text. Adopt changed runner code through
an explicit schedule restart at its work boundary, or a full stop/start.
**A wave that moves code between packages (a package rename is the canonical
case), changes schedule templates, moves or renames files that an in-store copy
loads or calls at run time, or otherwise touches an import
surface must also close the loop on in-store schedule copies.** A rollout
never updates them (above), and a stale copy only surfaces at its next fire
(crash-loop auto-pause once it cannot stay up, or a failure that folds
into an unrelated exit code — 2026-10-01: a moved `scripts/` target read as
"new candidates"). Before trusting `schedules`,
verify and redeploy: run a full dry-import sweep over every in-store copy
(py_compile + top-level imports only — never a real fire; it does not
execute run-time paths, so after a file move check the copies' referenced
paths directly) and redeploy drifted
copies through the same `ava schedules update <name> --script-file <template>`
path. Tooling lives on the host that runs the weekly sweep (`~/.ava/sched-dry-import/run_dry.sh`;
the weekly `sched-dry-import-weekly` backstop schedule red-reports to its
operator).
On agent-runners it also runs capability preflights: a headed Chrome (when the browser
is enabled, see below), a **probe of the configured cross-machine transfer backend**
(`AVA_CROSS_MACHINE_TRANSFER_BACKEND`, `drive` by default), and **the ability to open
and merge pull requests on the memory pool repo** (the nightly memory consolidation
runs `gh` + `git push` on each machine; the gate `_ensure_github_pr` →
`base/deploy/git/github_pr.py:github_pr_blocker` fails loud unless `gh` is installed,
authenticated, and has write access to the pool repo). The transfer probe + GitHub-PR
gate are **split-deployment-only**: both are auto-skipped when this unit also carries
`gateway` (a single box has no peer to hand files to and consolidates memory locally);
the GitHub-PR gate stays a hard fail (a missing memory-sync capability silently breaks
consolidation), while the transfer backend is never a blocker — a split agent-runner
that does not want the probe can set `AVA_CROSS_MACHINE_TRANSFER_BACKEND=none`
(the old `AVA_REQUIRE_GOOGLE_DRIVE` opt-out was removed with the hard requirement). A
host whose memory must stay on-box instead runs `AVA_MEMORY_KEEP_LOCAL=true`: the pool
becomes a local-only git repo (no remote, no push / pull / PR), and the GitHub-PR gate
is skipped regardless of role. The transfer probe
(`_ensure_cross_machine_transfer` → `base/host/converge/google_drive.py:find_writable_google_drive`)
is how the fleet does cross-machine file transfer without a relay when Drive is present:
every agent-runner mounts the same Google Drive account, so an agent hands a file to
another machine by dropping it in its local Drive folder (the synced `My Drive` area —
the mount root is not writable) and passing the path; the peer reads it from its own
Drive folder once it mirrors over. The
probe verifies participation with a write+read+delete round-trip; a split agent-runner
without a signed-in Drive starts anyway with a warning (files move via the gateway
upload path, GitHub Releases, or IM file bridges instead). The probe checks the per-OS
Drive locations: macOS
`~/Library/CloudStorage/GoogleDrive-<account>/My Drive`, WSL the Windows Drive-letter
mount surfaced under `/mnt/<letter>/My Drive` (identified by the `My Drive` subfolder, so
a plain `/mnt/c` never matches), and native Linux an rclone / `~/GoogleDrive` mount.

Service selection is durable. A bare `ava start` retains it; explicit
`--only-service`, `--disable-service` or `--all-services` changes the selection.
The root admits and probes the complete selected roster. There is no watchdog
controller that pulls a checkout or chooses a release on the application's behalf.

A failed update half is recovered by fixing its cause and rerunning that half:
each half of `python -m cli.fleet_update` is idempotent
([Updating a networked cluster in source mode](#updating-a-networked-cluster-in-source-mode)).

Ordinary maintenance holds retain their separate operator procedures in
[graceful maintenance](graceful-maintenance.md). Generic recovery is not a way
to forge process closure; do not resurrect a removed updater or bootstrap entry.

Commands in the "long-running processes" / "E2E tests" sections below default to cwd = `$AVA_HOME/source/` (prod context). Dev work goes through `~/Ava/.worktrees/<task>/`.

## $AVA_HOME, installed packages, and node capabilities

The `$AVA_HOME` directory tree, the `PluginsConfig` / `installed.json` schemas,
the `ava plugins` / `ava skill` / `ava mcp` command surface, and the
capability -> service model are structure, not procedure. They live in the OKF
nodes of the packages that own the code:

| What | Node |
|---|---|
| `$AVA_HOME` layout, what derives from the home | `base/paths/docs/paths.ava.okf.md` |
| plugin enable config (`plugins_config.json`) | `base/packages/plugins/docs/enable_config.ava.okf.md` |
| `installed.json` schema, installable shapes, the scanner gate | `base/packages/extensions/docs/install_registry.ava.okf.md` |
| `ava plugins` / `skill` / `mcp` verbs, MCP merge layers, secret channel | `cli/commands/extensions/docs/packages.ava.okf.md` |
| machine name, capability set, `machines` table, spawn-target 400 invariant | `base/cluster/docs/machine.ava.okf.md` |
| which services each capability contributes | `services/docs/services.ava.okf.md` |

Three operational consequences worth stating here:

- **Install external packages on an agent-runner, not a gateway.** Skills and
  MCP servers are consumed inside the agent process, and agents only run on
  runner units — a package dropped into a gateway-only host is never scanned.
- **Installs are per machine.** To install somewhere else, spawn an agent there
  (`ava.agents.spawn(machine=...)`); nothing is pushed from the cluster.
- Per-host inventory (private-network addresses, public IPs, SSH key paths) is
  operator-specific and belongs in your own deployment notes, not here.

## Long-running processes: one service per session

Ava's long-running **daemons** are each kept alive in their own named session — never crammed into one
session with multiple windows. On POSIX they run as **detached native processes** (double-forked onto init by
the process supervisor, `base/sessions/posixproc.py`). Agent
interactive shells / watchers each run in their own detached pty host
(`base/sessions/pty/` — one `pty.fork()` `bash -l -i` + pyte screen capture +
byte transcript under `$AVA_HOME/logs/` per host, session ops over the
session's own socket at `$AVA_HOME/run/pty/<name>.sock`; hosts reparent to
init at creation, so they are outside the service roster. Pause and update
preserve them; full stop explicitly closes them).
A host runs the enabled service specs for its capabilities. Agent execution
always belongs to `agent-host`; each agent is a scheduled turn, not a separate
OS process. A null per-agent PID is expected. Inspect host membership, claim
progress and turn events for execution state. Verify actual process/session
inventory as well as the desired roster when stopping an older release.

Session names follow the pattern `ava-<service>` (composed by
`base/cluster/derive.py:session_name()`; neither machine nor cluster is encoded —
per-home hosting scopes them: the `$AVA_HOME/run/sessions/` records for native ones, the PTY
supervisor socket for agent shells / watchers).

A standard `/healthz` daemon declares no healthcheck module. Its Healthcheck cell reads
*roster identity probe*: the probe `ops/roster/healthz.py` derives from the service name,
which asks `/healthz` on the daemon's health-port slot and accepts it only when the
body's name, home and pid are this unit's own daemon.

<!-- lint:roster-table -->
| Service (suffix)         | Runs                                     | Healthcheck |
|--------------------------|------------------------------------------|-------------|
| `gate` (gateway only) | `.venv/bin/python -m services.gate.daemon` (root-owned public HTTP entry on `:3000`: auth-gates and proxies the Next.js `frontend`, and renders the login and Service unavailable pages) | `services.healthchecks.gate` (`GET /__ava/healthz` identity-verified) |
| `gateway` ★ (gateway only) | `.venv/bin/python scripts/start_gateway.py` (FastAPI 0.0.0.0:8000) | `services.healthchecks.gateway` (HTTP `/api/agents` 200) |
| `ops` (agent-runner only) | `.venv/bin/python -m services.agent_ops.daemon` (inbound server on 0.0.0.0:<ops_port>; the gateway POSTs each cluster op to `/ops`, dispatched in-process via the gateway ops_* modules) | roster identity probe (`/healthz` :8113) |
| `agent-host` (agent-runner only) | `.venv/bin/python -m services.agent_host.daemon`: one host schedules local turns with bounded concurrency, shared workload/control pools and per-agent context. Idle has no task. `/stats` reports active turns and cache use. Normal update drains claim, checkpoint, continuation and execution resources before stopping this service. Uncancellable tasks are reported; killing the whole host interrupts every active turn on that runner. | roster identity probe (`/healthz` :8114) |
| `page-server` (agent-runner only) | `.venv/bin/python -m services.page_server.daemon` (supervisor of page servers: every open `agent_pages` row whose serve_dir is set — `ava.ui.serve()` pages — gets exactly one detached page server process on this host, spawned from the row's serve_dir on the row's port; rows that close get their server killed, while serve() pages stay open across an agent terminate. Truth source is the `agent_pages` table, not the session tree — a rollout's session rebuild does not kill page servers, an agent restart does not orphan them; a second loop scans every open show() page of the machine every `AVA_HEARTBEAT_INTERVAL_SECONDS` and closes the ones whose agent-owned server died, telling the owner once; the two loops share one `TaskGroup`) | roster identity probe (`/healthz` :8112) |
| `labeler`                | `.venv/bin/python -m services.labeler.daemon` (auto label generation) | roster identity probe (`/healthz` :8103) |
| `im-bridge`              | `.venv/bin/python -m services.im_bridge.daemon` (IM frontends: Telegram; WeChat iLink / Feishu adapters shipped but **production-disabled since 2026-08-06** — `AVA_IM_DISABLED_ADAPTERS=weixin,feishu`) | roster identity probe (`/healthz` :8111) |
| `heartbeat` (gateway only) | `.venv/bin/python -m services.heartbeat.daemon` (every `AVA_HEARTBEAT_INTERVAL_SECONDS`, default 15 min, scans `idling` agents past `AVA_HEARTBEAT_IDLE_THRESHOLD_SECONDS` that have not called `ava.self.pause_heartbeat()` and INSERTs a `heartbeat` check-in inbound; cluster-wide — the inbound-insert trigger wakes the agent on any machine, so it runs once on the gateway, not per agent-runner; resident loops under one `TaskGroup` with a progress tracker each: check-in dispatch, agent liveness, and the hourly completion-notice digest delivery every 60 s (`completion_notice_events` -> one `system:completion-digest` chat per agent and hour, keyed so a crash between delivery and mark is exactly once)) | roster identity probe (`/healthz` :8107) |
| `delivery-watchdog` (gateway only) | `.venv/bin/python -m services.delivery_watchdog.daemon` (six jobs on four resident loops, default 0.5s per `AVA_DELIVERY_WATCHDOG_INTERVAL_SECONDS`: **(1) wake dispatch** — re-publishes the Redis wake (with the wake-key breadcrumb) for every `pending` inbound of an `idling` owner older than `AVA_DELIVERY_WATCHDOG_DISPATCH_THRESHOLD_SECONDS` (default 1s), collapsing the lost-publish recovery from the claim loop's 30s recheck to ~1.5s; host-gated: re-dispatch and poisoning apply only while the owner's `machine_probe` verdict is fresh (machine graded online by the two-consecutive-failure rule, agent host alive — the host check is excused inside the one-failure grace window, where a failed probe nulls it — `AVA_DELIVERY_WATCHDOG_HOST_STALENESS_SECONDS` default 120s), a stale, graded-offline or (outside that window) host-less verdict freezes the row (no counter burn, no poison) and it resumes once the verdict is fresh (task #4872 route D); constant ~2 qps load, independent of fleet size; **(2) stall alerting** — WARNINGs chat inbounds still `pending` past `AVA_DELIVERY_WATCHDOG_THRESHOLD_SECONDS` (default 30s) whose owner is `idling`/`terminated`, once per row while stuck, with a `delivery_stalled` event emitted to the unified `events` stream; **(3) terminated-owner resurrect retry** — re-runs `resurrect_if_terminated` for each terminated owner holding a post-death `pending` chat younger than `AVA_DELIVERY_WATCHDOG_STALE_CLAIMED_THRESHOLD_SECONDS` (default 24h; Task #689 G4) — including the corpse reaper's committed crash-recovery wake (task #4039), with persisted per-agent cooldowns (`delivery_watchdog_attempts`) and the escalating `resurrect_failed` wake-suppression ladder (30-minute exponential window capped at 24 h, after five consecutive failed attempts); **(4) stale-inbound dead-letter sweeps** — every 30s flips `claimed` then `pending` chat rows of terminated owners past the same stale threshold to `done` (Tasks #654/#2049), so the sweep and the G4 trigger share one age gate; **(5) stalled crash-marked recovery request** — escalates a chat still `pending` past the stall threshold whose owner is a crash-marked idling corpse over the internal `recover-crash-marked-v2` path (one request per owner, 60s cooldown, `delivery_recovery_decision` per decision, gated by `AVA_DELIVERY_STALLED_RECOVERY_ENABLED`; Task #3618); **(6) hosted-turn liveness recovery** — confirms any hosted `running` agent whose DB activity is older than the 2400 s wedged-agent budget (`wedged_agent_inbound_age_seconds`) against the agent-host's 15 s Redis progress heartbeat (missing heartbeats or stale per-turn marks = wedged), then force-terminates the incarnation and queues the marked `hosted_turn_recovery` chat so guarded resurrection survives restarts; one attempt per agent per 10-minute cooldown, `host_turn_stall_detected` evidence (Task #1712). `running` owners are never dispatched or alerted (mid-turn queues are normal). Gate cluster-level on/off with `AVA_DELIVERY_WATCHDOG_ENABLED`) | roster identity probe (`/healthz` :8110) |
| `ttl-reaper` (gateway only) | `.venv/bin/python -m services.ttl_reaper.daemon` (two resident loops under one `TaskGroup`, a round every `AVA_TTL_REAPER_POLL_INTERVAL_SECONDS`, default 60s; a loop that raises ends the process and root restarts it. **`sweep`** (database only) — terminalizes `agent_pages` past `expires_at` (`page_ttl_expired`, `PageClosed` published), deletes expired `web_sessions`, auto-resolves expired `agent_notices`, reminds then reaps impersonation leases, and runs three slow phases whose clocks live in `maintenance_state` so a restart resumes them: the `schedule_fire_log` retention prune (`AVA_SCHEDULE_FIRE_LOG_CLEANUP_INTERVAL_SECONDS`, daily, keeps the newest claim per schedule), the torn lifecycle-pointer scan and the absent-machine fence settle (both hourly). **`remote`** — kills TTL-expired persistent shell sessions (`agent_shell_ttls`, watchers included) on their home machines with a `shell_kill` op (machines concurrently, one machine's rows in order, a row is deleted only on a definitive verdict, `shell_ttl_expired`), then retries stale `work_failed_events` deliveries. Each dispatch and each delivery runs under a deadline sized from the RPC client's budget. Always runs: no config gate; stopping it leaves the gateway untouched) | roster identity probe (`/healthz` :8121) |
| `schedule-manager` (gateway only) | `.venv/bin/python -m services.schedule_manager.daemon` (two resident loops under one `TaskGroup`; a loop that raises ends the process and root restarts it, the schedule sessions survive it. **`reconcile`**, every 5 s: desired (enabled `schedules` rows) against actual (live `ava-schedule-<id>` PTY sessions) — launches the missing ones under the crash backoff (`launch_count` / `next_launch_at` on the row, a launch claimed with one conditional UPDATE) and the breaker (5 launches, then `status='error'`), reaps the unwanted, closes orphaned run rows, and raises the two-hour no-session alert once per outage (`not_live_since` / `stall_alerted_at`). **`requests`**, every second: consumes `schedule_sync_requests` rows the API leaves on start / stop / restart / script edit / delete (kill, relaunch if enabled, clear the backoff), staying queued during a maintenance hold. Seeds the built-in schedules at start (`AVA_PROVISION_BUILTIN_SCHEDULES`). Refuses to start from a checkout that does not own the home) | roster identity probe (`/healthz` :8122) |
| `task-maintenance` (gateway only; **registered by the `ava_fleet` plugin**, not core — see `ava_builtins/plugins/ava_fleet/services.py`) | `.venv/bin/python -m ava_builtins.plugins.ava_fleet.task_maintenance.daemon` (every `AVA_TASK_MAINTENANCE_INTERVAL_SECONDS`, default 5 min, reminds owners of overdue in-progress tasks past their `remind_interval_seconds` window via a `chat` inbound; after `AVA_TASK_ESCALATE_N` (default 3) unanswered reminders, notifies the parent task's owner. Cluster-wide, runs once on the gateway. Discovered whenever the `ava_fleet` plugin code is present; gate its cluster-level on/off with `AVA_TASK_MAINTENANCE_ENABLED`) | roster identity probe (`/healthz` :8108) |
| `events-maintenance` (gateway only) | `.venv/bin/python -m services.events_maintenance.daemon` (every `AVA_EVENTS_MAINTENANCE_INTERVAL_SECONDS`, default 1h. Each pass incrementally maintains the Since-Birth day-grain rollups — `agent_metrics_daily` / `agent_model_tokens_daily` (the durable token+cost ledger) — from **Loki** (the unified event stream's live store): one union-family count probe compares retained candidate days with `rollup_day_state`; missing, failed, count-changed, and the latest `AVA_EVENTS_ROLLUP_LATE_WRITE_LOOKBACK_DAYS` (default 1) get a full-day overwrite, while clean days avoid the fourteen aggregate queries. The scan clamps to Loki's 84h retention floor (an outage longer than retention loses those days' Loki aggregates — logged loudly; the filtered `events-YYYYMMDD.rollup.jsonl` mirror (90-day retention by default, tunable via `AVA_EVENTS_JSONL_ROLLUP_RETENTION_DAYS`) then automatically repairs older ledger-watermark gaps: zero-known-row files fail loudly and are not counted as replayed, missing files remain unrecoverable; pre-LGTM history was backfilled once by the llm-cost-rollup-columns migration from the frozen PG archive), uses its own capacity-one Loki budget, and stops between days at `AVA_EVENTS_ROLLUP_PASS_DEADLINE_S` (default 1200), leaving untouched/failed state for the next pass. Today is served live by the readers (whole-life cost = ledger + Loki tail from the watermark). Full-day overwrite upsert keyed on the PK ⇒ idempotent; a zero-row indexed slice preserves existing ledger rows and marks the day failed for retry. Cluster-wide, runs once on the gateway — it owns the data plane. The rollup, JSONL replay, blob vacuum and hourly checkpoint size sample are unconditional — the PG `events` archive slices (partition rolling, retention, index governance) were removed with the task #1281/#1823 cleanup. The current baseline omits that archive. The checkpoint trim opt-in was retired on 2026-09-30 under the never-delete ruling; its reaper implementation remains unscheduled pending separate retirement. A third loop samples `max(agents.id)` once a minute for the `agent_registry` growth gauge; a fourth, only on a unit holding `GRAFANA_ADMIN_PASSWORD`, reconciles stored Grafana alert rows against Grafana's active Alertmanager view every five minutes; the loops share one `TaskGroup`) | roster identity probe (`/healthz` :8109) |
| `milvus`                 | `.venv/bin/python -m services.milvus.daemon` (`milvus-lite server` gRPC :19530, data dir `~/.ava/milvus-data/`) | `services.healthchecks.milvus` (TCP probe :19530) |
| `memory-indexer`         | `.venv/bin/python -m services.memory_indexer.daemon` (watchdog fs watch `~/.ava/memory/` + Gemini Embedding 2 → milvus collection) | roster identity probe (`/healthz` :8105) |
| `memory-search`          | `.venv/bin/python -m services.memory_search.daemon` (uvicorn on 127.0.0.1:19531 serving the exact-search store — in-memory matrix + npz persistence; the gateway and the indexer call it over HTTP when `AVA_MEMORY_SEARCH_BACKEND=numpy`) | `services.healthchecks.memory_search` (real POST /search probe :19531) |
| `frontend`               | `cd ui/web && NEXT_PUBLIC_GATEWAY_PORT=<AVA_GATEWAY_PORT> npm run build && npm run start -- -p <app_port>` (Next.js prod build, **loopback-only bind** (`next start -H 127.0.0.1`); off-box browsers reach it only through the fleet UI gate on the entry port `:3000` — see Private-network deployment. The build-time port is injected from `AVA_GATEWAY_PORT` so the browser dials the gateway on the right port even when it is not the default 8000) | `services.healthchecks.frontend` (curl) |
| `pg-backup` (gateway only) | `.venv/bin/python -m services.backup_scheduler.daemon` (cluster-clock daily dump schedule with bounded retry and owned, cancellable job processes; after the Sunday 03:00 successful dump, runs one isolated logical restore drill; `/healthz` reports last-success age) | roster identity probe (`/healthz` :8116) |
| `browser` (agent-runner only, auto-detect display; opt-out `AVA_BROWSER_ENABLED=false`) | `.venv/bin/python -m services.browser.daemon` (headed real Chrome, dedicated profile `~/.ava/chrome-profile/`, CDP :9222) | `services.healthchecks.browser` (HTTP probe `/json/version` :9222) |
| `otel-collector` | `<otel-collector-dir>/otelcol-contrib --config <otel-collector-dir>/config.yaml` (native Go binary installed by converge on the `lgtm-host` gateway and pure runners; unmarked gateway homes skip it; the gateway fans out only its cluster's labeled resources, pure runners relay with bearer auth; traces mirror locally; trace/log queues are bounded and file-backed (1 GiB on-disk cap each) while metrics use bounded memory; every full queue rejects the newest batch without waiting, and every exporter gives up after a bounded 15-minute retry window) | `services.healthchecks.otel_collector` (valid empty OTLP POST must return 2xx on the local `AVA_TELEMETRY_OTLP_PORT`; both that port and `AVA_OTELCOL_METRICS_PORT` holders must resolve to this collector binary and its live session record, otherwise only a verified stale same-binary holder is reclaimed; failed listener inspection is reported as unavailable and never triggers respawn) |
| `loki` (observability station) | Pinned native Loki under ava-root | `services.healthchecks.lgtm` (owned listener + `/ready` success) |
| `prometheus` (observability station) | Pinned native Prometheus under ava-root | `services.healthchecks.lgtm` (owned listener + `/-/ready` success) |
| `grafana` (observability station) | Pinned native Grafana under ava-root | `services.healthchecks.lgtm` (owned listener + `/api/health`, database ready) |
| `browser-mcp` (agent-runner only, gated with `browser`) | `.venv/bin/python -m services.browser.mcp_daemon` (one shared `chrome-devtools-mcp` upstream attached to the headed Chrome, multiplexed over a Unix socket `~/.ava/chrome-mcp.<cdp_port>.sock` to every agent's chrome bridge — serial, with per-connection page affinity so one Chrome client is shared instead of one per browser-using agent) | `services.healthchecks.browser_mcp` (Unix-socket `list_tools` probe) |
| `computer-mcp` (agent-runner only, platform-gated: signed permissions helper enabled + capable, AF_UNIX transport, non-Windows host — Windows is the phase-3 pilot) | `.venv/bin/python -m services.computer.mcp_daemon` (computer-use executor: every desktop action through the signed permissions helper — serialized machine-wide, screen-coordinated (lease + FIFO queue + `release_control`), Vision OCR on snapshots, audited as `computer_action` + `computer_session_start/end` events, served over `~/.ava/run/computer-mcp.sock`) | `services.healthchecks.computer_mcp` (Unix-socket lock-free `ping` probe) |
| `mcp-daemon` (agent-runner only) | `.venv/bin/python -m ava.mcps._daemon` (ONE shared MCP daemon per machine, serving every agent over `~/.ava/run/mcp_daemon.sock` — sessions isolated per client connection, replacing the old one-daemon-per-agent children) | `services.healthchecks.mcp_daemon` (Unix-socket `ping` probe) |

The gate preserves the browser `Host` while proxying to the loopback frontend,
so the frontend CSP derives the same host that its API client uses. A TLS or
reverse proxy before the gate must overwrite `X-Forwarded-Host` and
`X-Forwarded-Proto` with the public browser origin; the gate relays those
headers only when present. Use lowercase `http` or `https` for
`X-Forwarded-Proto`; the frontend normalizes other casing before deriving its
CSP origin. The gate also relays the frontend CSP and static browser-security
headers to the public response.

#### Optional HTTPS browser entry (HTTP/2)

Use a browser-facing HTTPS reverse proxy when several visible console windows
exhaust HTTP/1.1's six connections with SSE. Keep `AVA_GATEWAY_URL` and the
existing listener ports as the runner/control-plane addresses. On the gateway
unit, set `AVA_BROWSER_ORIGIN=https://<entry-host>` (an origin only, no path).
The normal frontend build derives its browser setting from this field; do not
write `NEXT_PUBLIC_*` overrides. Deploy the change through the normal update
path so the gateway, gate login page and rebuilt frontend agree. If an explicit
`AVA_GATEWAY_CORS_ALLOWED_ORIGINS` list exists, add the exact new origin there;
the explicit allowlist remains authoritative.

This setting currently requires a source-built frontend. Retained standalone
images do not record a browser origin in their build manifest; their launcher
refuses a nonempty setting instead of silently serving a mismatched bundle.

Only a browser visiting that exact origin uses same-origin API/SSE. Direct IP
frontend URLs keep their existing gateway-port routing for staged verification
and rollback. Login through the new HTTPS origin sets a Secure session cookie;
direct HTTP login retains its existing policy. A hostname change requires a
new browser login. Forward the original Host and overwrite X-Forwarded-Host /
X-Forwarded-Proto at the trusted entry; Uvicorn's trusted proxy boundary stays
loopback, never `forwarded-allow-ips=*`.

Route `/api` and its descendants, `/pages` and its descendants, and `/grafana`
and its descendants directly to the gateway; all remaining requests go to the
existing gate. The gate buffers frontend
responses and must not proxy SSE. Preserve route prefixes, query strings,
cookies, redirects, and immediate `text/event-stream` delivery. Keep the entry
supervised independently of rollout service teardown.

If the host's private network already provides a managed HTTPS entry (listener,
certificate renewal and persistence), it can take this role without a second
proxy daemon. Keep that entry reachable only on the private network, never
publicly exposed. If its certificates come from a public CA, issuance
publishes the node's full DNS name in Certificate Transparency. Save the
entry's current configuration first and refuse to overwrite an occupied HTTPS
port. For example, for gateway 20016 and gate 20017 on an otherwise unused
HTTPS port 443, add exactly four handlers:

| Path on `https://<entry-host>` | Upstream |
|---|---|
| `/api` | `http://127.0.0.1:20016/api` |
| `/pages` | `http://127.0.0.1:20016/pages` |
| `/grafana` | `http://127.0.0.1:20016/grafana` |
| `/` (everything else) | `http://127.0.0.1:20017` |

An entry that strips the mount prefix needs the upstream URL to restore it, as
above. Make the handlers persist across restarts of the entry. Preserve its
other handlers and TCP relays; never reset the whole entry to roll this back.
Remove only the four added handlers. The direct IP entry remains available
throughout.

Acceptance: verify ALPN negotiates `h2` from the user's machine with normal
certificate verification; load several console windows and inspect the real
browser Network protocol and queueing; check login, tree, inspector, SSE,
Grafana/pages and maintenance-state requests. API headers alone are not proof
that the browser used HTTP/2. Full test suites run in CI; local verification
uses the affected tests and the operator's authorized browser smoke.

### Backup and recovery posture

The recovery points are the daily encrypted logical dumps (`pg-backup`, due at
`AVA_BACKUP_HOUR` cluster time, the newest `AVA_BACKUP_KEEP` kept in
`$AVA_HOME/backups/db/`, published off-site under `ava-logical/` when
`AVA_BACKUP_OFFSITE_ENDPOINT`, `AVA_BACKUP_OFFSITE_BUCKET` and
`AVA_BACKUP_OFFSITE_CREDENTIALS_FILE` are set through `ava config set`), proved
by the weekly isolated logical restore drill. The self-written PITR stack was
deleted (`decisions/2026-10-02-delete-the-self-written-pitr-stack.md`). Point-in-time
recovery exists only while WAL-G is on (["WAL-G archiving"](#wal-g-archiving) below):
`ava backup walg restore`, proved weekly by the recovery drill. Never state a
recovery point newer than the last published dump unless the latest drill has passed.

Backup operations (the daily dump and the weekly logical restore drill) each
run as one owned worker group of their kind. A failed or cancelled operation
whose group closure was proven -- including an `ava stop` during the nightly
dump -- is quarantined under its kind's `$AVA_HOME/backups/quarantine/<kind>/`
(request, logs, `failure.txt`, receipts; plaintext database material
removed), raises the
`ava-ops-backup-operation-quarantined` warning, and the next scheduled run
proceeds. Unproven closure (or a controller killed mid-operation) blocks only
that kind and raises `ava-ops-backup-operation-blocked`:

```bash
ava backup operations status            # blocked kinds, reasons, newest quarantine entries
ava backup operations retire            # preview: re-prove closure of each blocked operation
ava backup operations retire --confirm  # quarantine every proven one; the kind proceeds
```

Closure covers the PostgreSQL children that `setsid` out of the worker group:
retire also requires every recorded postgres birth dead and no process working
inside a receipted data directory. Each refusal names its type and the PIDs
involved (`worker-present`, `group-members`, `postgres-family-alive`,
`birth-unrecorded`, `unverifiable`, `quarantine-failed`); wait for them (or stop
them) and retry. A controller killed while launching can only be proven after
a reboot. A closed operation whose quarantine keeps failing shows as blocked
and `retire --confirm` retries it. An upload-interrupted dump keeps
its complete encrypted artifact in quarantine: restore from it directly
(`.agents/skills/operating-ava-cluster/references/db-restore.md`) or copy it
into `backups/db/` (0600); the next scheduled run dumps again.

All sessions have cwd set to the prod path `~/.ava/source/` (see "Prod and dev clone paths" above).
Session commands run under `bash -lc` (#476) — the login-shell flag pulls in the user's
`~/.bash_profile` / `~/.profile` so `~/.local/bin` (where `uv` typically lives on WSL / Linux) is on
PATH without needing a sudo-installed symlink. macOS dev hosts already inherit login-shell PATH from
Terminal.app; the change is load-bearing only for Linux / WSL agent-runners.

The agent host is the `ava-agent-host` service. Agent identities are counted
from this machine's non-terminated `agents_meta` rows; idle and paused identities
remain visible. Enumerate service sessions through the native supervisor and
persistent shells through `ava sessions list` / the shell backend. No per-agent
main-process record participates in the current runtime.

### Emergency PTY allocation freeze

Freeze new PTY allocation before a host-wide inspection or bounded cleanup:

```bash
ava pty freeze --holder idle-fix-operator --reason "manifest and bounded cleanup"
ava pty status
```

The command takes the host allocation lock and prints a random generation
token. Its acknowledgement is the boundary: an allocation already in flight is
ready and recorded before the command returns, while every later missing-name
allocation is refused before a host is forked. All co-located `$AVA_HOME`
values cross the same gate. A request for an already-live name remains an
idempotent success while frozen. Refused requests remove their 0600 environment
handoff files; they may leave harmless gaps in an agent's monotonic shell IDs,
which are never rolled back or reused.

The boundary has a deliberate reconciliation effect at **freeze**, not at
resume. The allocation command does not directly kill an existing PTY, but the
next ScheduleManager tick (about five seconds) reaps every schedule PTY from
the preceding generation, interrupts any open schedule run, and leaves the
enabled schedule as the current desired state for a later replacement. A
watcher session (`ava.watcher.at/cron/launch`) is NOT part of this
reconciliation at all — it has no desired-state record and nothing rebuilds or
reaps it by generation (decisions/2026-09-27-watchers-are-never-restarted.md);
a preceding-generation watcher session simply keeps running (or not) exactly
as it would without the freeze, subject only to its own TTL deadline, an
explicit kill, or `ava stop`. This applies to an inspection-only freeze too:
it is not safe to assume that existing desired-state sessions (schedules)
keep running after the freeze acknowledgement.

Resume with the exact token printed by the freeze that this operator owns:

```bash
ava pty resume <generation-token>
```

A stale token cannot clear a newer freeze. `freeze`, `status`, and `resume` are
local host operations and remain usable while the gateway, Postgres, or Redis
is unavailable. A malformed marker fails closed; inspect the marker path shown
by `ava pty status` and perform an audited manual repair rather than treating
corruption as an implicit resume. Do **not** delete the marker: that changes the
current generation to `None` and can make the next ScheduleManager tick reap
every generation-bound schedule declaration (watchers are unaffected — they
have no generation-bound declaration to reap). Recover the original generation
UUID from any known-live PTY session record, rebuild a valid marker with that
exact UUID, and only then allow reconciliation to resume. If no record
establishes the UUID, leave the marker fail-closed and restore desired state
only after an operator has made the boundary explicit.

For a bounded host cleanup, keep the order explicit:

1. Stop or fence every reconciler that can create replacement sessions; this is
   required before freeze when the cleanup must be selective.
2. Freeze allocation and retain the returned generation token.
3. Snapshot the official PTY inventory and the durable desired state that will
   be rebuilt.
4. Terminate only the selected sessions through the identity-aware PTY API.
5. Verify that no later session start crossed the freeze boundary.
6. Rebuild the selected durable state.
7. Resume with the exact freeze token, then restore controllers one at a time;
   restore any capacity guard last.

### Canonical Codex workspace sessions

The Codex launcher in `ava-use-other-agents` owns one canonical
session per `(cluster, workspace, tool)`. Check it before starting work:

```bash
python ava_builtins/skills/ava-use-other-agents/scripts/spawn_codex.py \
  /absolute/workspace \
  --tasks-file /absolute/workspace/tasks.md \
  --work-file /absolute/workspace/work.md \
  --status
```

Every launch owns a generation record of its own, so several Codex sessions can
share a workspace; `--status` lists them all, and a launch first reclaims the
workspace's dead generations (expired, crashed, unsupervised, or owned by a
terminated agent). A record is either a supervised worker (task/work files plus an automatic
supervisor) or a file-less takeover (no files, no supervisor; the coding
session alone is its liveness signal).
Each ownership generation has a private
`$AVA_HOME/run/coding-tools/codex/<workspace-key>/<generation>/` state
directory (a takeover's app-server log) and a fresh numeric PTY identity.
Codex itself runs on the host user's `~/.codex` with per-session `-c`
overrides, and every launch prints its `codex_session`: after a full stop
closed the shell, `--resume <codex_session>` reopens that conversation, while a
rebuilt generation without it starts fresh from the task file, work log,
collaboration contract and Git state (a takeover from its inline briefing).

For a supervised generation the launcher starts a quiet supervisor. It closes
the full Codex PTY and terminalizes the record when `work.md` reaches `DONE` or
`HANDOFF`, the owner agent terminates, the Codex session crashes, the task
expires, or an operator cancels that exact generation. A takeover generation
starts no supervisor: explicit cancel and expiry are its stop paths. The default task TTL is
four hours and can be changed with `--ttl-seconds`; TTL is the fallback, not
the normal lifecycle boundary. Cancel only the generation printed by the
launcher or `--status`:

```bash
python ava_builtins/skills/ava-use-other-agents/scripts/spawn_codex.py \
  /absolute/workspace \
  --tasks-file /absolute/workspace/tasks.md \
  --work-file /absolute/workspace/work.md \
  --cancel-generation <generation-token>
```

A stale generation token cannot terminate a replacement owner.

### Shared browser (`browser` service)

A single headed, real Chrome shared by all agents on an agent-runner, so they can
browse / crawl like a person in a logged-in browser (and run arbitrary JS via the
chrome MCP tools, surfaced as `ava.mcps.chrome.<tool>(...)`). One
`ava-browser` service session owns the Chrome process
(`--remote-debugging-port`, dedicated profile `~/.ava/chrome-profile/`). A second
`ava-browser-mcp` session (`services/browser/mcp_daemon.py`) owns ONE
`chrome-devtools-mcp` attached to that Chrome and multiplexes it to every agent
over a Unix socket — so the heavy CDP client is shared, not spawned per agent
(each upstream's collectors buffer the WHOLE browser's traffic, so N upstreams
meant an N-fold duplication). The daemon is serial (one browser op at a time) and
keeps per-connection page affinity (it re-selects each agent's own page before a
page-scoped call, so concurrent agents never act on each other's tab through the
single shared selected-page) and applies the cold-start `navigate_page` fix (a
page-less navigate becomes `new_page`). The agent side is process-less since
2026-08: the shared MCP daemon (`ava/mcps/_daemon.py`) dials the daemon's socket
directly (`"shared": "browser"` in the chrome `.mcp.json`, in-daemon line client
`ava/mcps/_browser.py`) — the former per-agent stdio bridge
(`services/browser/mcp_wrapper.py`, ~63MB per agent) is no longer spawned. Both
sides derive the CDP port + socket path from `settings.browser_cdp_port`
(per-cluster).

- **Profile source — fresh vs. seeded from your daily Chrome**: the dedicated
  profile is normally created empty, so the agent signs in to every site itself.
  On the **first** `ava start` on a browser-capable host, when the profile is
  still absent **and** a human is at the TTY, converge's `_ensure_browser`
  (`services/browser/profile.py:ensure_browser_profile`) offers to seed it by
  **copying your daily Chrome profile** (macOS `~/Library/Application Support/Google/Chrome`,
  Linux `~/.config/google-chrome`) into `~/.ava/chrome-profile/` instead. Copying
  hands the agent your full logged-in identity (cookies, sessions, saved
  passwords, signed-in accounts) so it acts as you without a re-login — a security
  trade-off, so it is opt-in behind an explicit confirmation and the default is a
  fresh profile. The copy excludes lock/socket files (`Singleton*`) and
  regenerable caches, reports its size first, and refuses while Chrome is still
  running (copying live SQLite risks a corrupt import — quit Chrome and retry).
  **Guardrails**: any existing profile directory is never touched, including an
  empty or partial first copy (idempotent across restarts; prod's multi-GB logged-in
  profile survives every start); non-interactive
  paths (root revival, boot autostart, a `cli.fleet_update` start) never prompt and
  always take the fresh default; a host with no daily Chrome degrades silently to
  fresh.
- **On by default with auto-detect**: `AVA_BROWSER_ENABLED` defaults to true.
  On a headless machine (no `$DISPLAY` / `$WAYLAND_DISPLAY` on Linux), the
  converge step prints a warning and skips the browser — `ava start`
  proceeds normally without it. On a headed machine (macOS / Linux with display),
  the browser session and its healthcheck engage automatically. Set
  `AVA_BROWSER_ENABLED=false` to explicitly opt out. The display verdict is
  computed consistently across processes: `$DISPLAY` / `$WAYLAND_DISPLAY` are
  passed through both env builders (`base/sessions/env_forwarding.py`) —
  forwarded into every daemon service session — so every service sees
  the same display the operator's shell does. Without this a headed Linux / WSLg
  host would strip the display and wrongly skip (and never revive) a browser it
  can actually run.
- **Capability-gated at two layers, observably**: (1) `_services_for_roles`, the
  watchdog's `_checks_for_capability`, and `agent/warmup.py` all gate on
  `browser_incapability()` (`base/host/system/probes.py`) — the single source of
  the display + Chrome-binary + npx check, returning the reason a prong is missing
  (or None when capable). A host missing any of the three never starts the browser
  session or its healthcheck, and warmup never polls a CDP port that will not
  exist. The reason is surfaced, not swallowed: `ava status` shows the browser row
  tagged `skipped: <reason>` (via `_services_for_roles_annotated`) instead of
  hiding it, and `ava start` prints it on the console — these two are the
  operator's pull-surfaces. The watchdog (debug, every 60s round) and warmup
  additionally log it into their own logs as a secondary breadcrumb, not a peer
  surface. (2) The daemon's `main()` still calls `assert_browser_capable()`
  (which raises the same `browser_incapability()` reason) as a safety net for
  direct invocation.
- **Service-owned — don't start Chrome by hand**: the `ava-browser`
  session is the single owner of the CDP port and `~/.ava/chrome-profile/`. A
  manually-launched Chrome on that profile takes the singleton profile lock, so
  the daemon's Chrome forwards-then-exits and the session dies — `ava
  status` then shows the browser row `✗` while the hand-started Chrome keeps
  answering `/json/version`, so the healthcheck reads it as alive and never
  revives the session. The daemon guards the collision: `main()` probes the CDP
  port first and refuses with a clear message rather than exec'ing a second
  Chrome into the lock. To (re)take service ownership, stop the squatter, then
  `ava start` (or let the next root health round revive the session once the port is
  free) — and the refusal message now names that remedy itself. When the squatter
  is one of *ours* (a Chrome left outside the session by a `SingletonLock`
  handoff), `ava stop --force` sweeps it, so there is no pid hunt: it kills
  every Chrome running on this cluster's `--user-data-dir`. A Chrome on some other
  profile is deliberately left alone — it cannot be positively identified as ours,
  and the operator's own browser is the thing that must never be killed — so that
  one is still quit by hand.
- **Browser lifetime**: pause, update and restart preserve the running
  `ava-browser` session. Default `ava stop` closes it; `--keep-service browser`
  retains it. The profile and its logins survive either choice. `ava start`
  reuses a retained session and relaunches a stopped one. Explicit force stop
  and cluster destroy additionally sweep owned Chrome processes outside the
  recorded session tree (`services/browser/orphan.py`).
- **Upgrade impact**: all headed agent-runner hosts that upgrade without having
  previously set `AVA_BROWSER_ENABLED` will auto-launch a headed Chrome on the
  next `ava start`. Chrome binds CDP to loopback:9222 only; the profile starts
  empty (no cookies). To prevent the window, set `AVA_BROWSER_ENABLED=false`
  before upgrading, or after the first start.
- **macOS runtime readiness**: static capability detection still treats macOS
  as display-capable, but the browser daemon does not launch Chrome until the
  service account owns the active console GUI session, its `launchctl gui/<uid>`
  namespace exists, and the login Keychain answers the read-only
  `security show-keychain-info` query. An SSH- or boot-triggered session that
  lacks any prerequisite stays alive and retries every five seconds instead of
  starting Chrome without encryption material. The browser probe and healthcheck
  expose that state as **DEGRADED** and preserve the waiting session rather than
  respawning it. **Exception — wrong launch domain**: a daemon launched or
  respawned from an agent/SSH chain runs outside the GUI login session
  (`launchctl managername` ≠ `Aqua`), where the Keychain is unreachable no
  matter how ready it is; the marker records that as `context_missing`, and the
  healthcheck stops the stuck session and kickstarts the cluster's GUI-domain
  autostart job (at most twice per episode, 600 seconds apart), deferring its
  own in-context rebuild while the relaunch lands; every automatic browser
  rebuild takes that same GUI-domain route when the chain is outside the
  session (task #3346), and `ava start` warns loudly when an agent-runner host
  is started from a chain outside the GUI login session — and (task #3348)
  hands such an operator-shaped start's bring-up to the GUI-domain job instead
  of running it in place, waiting with the normal readiness contract. The
  caller releases its local lifecycle lock and maintenance authorization
  before launching or observing that job; the GUI child owns readiness and
  resume. A failed GUI launch refuses startup instead of launching services
  in the wrong domain;
  `AVA_START_GUI_HANDOVER=0` restores the warn-only behavior. If the wait marker cannot be
  written, the probe and healthcheck use the same bounded read-only readiness
  check instead. The gate never
  unlocks a Keychain or changes Chrome data;
  `Local State` receives existence, permission, and mtime checks only,
  with warning-only results.
- **First login is the user's job**: the headed window opens on the host's
  desktop; **you** sign into the target sites (e.g. Google / Xiaohongshu) once — the
  agent does not (and cannot) log in for you. The dedicated profile persists the
  session across restarts.
- **Profile isolation**: `~/.ava/chrome-profile/` is separate from your daily
  Chrome profile (isolated cookie jar; signing it into Google does not evict your
  daily profile's sessions). It holds real logins — any agent on the host acts as
  those identities, so log in only what is needed; use a separate account for
  hardest isolation.
- **Display**: needs a real display (fine on a macOS desktop host); the Chrome
  window is visible on that host's screen.
- **Verification is manual**: live-browser use (driving a real site) is checked by
  hand, like the other real-MCP-server integrations — not in CI.

### Deployment footprint & memory

Ava retains a native Postgres/Redis data plane, LangGraph checkpoints, and a
Next.js frontend. One agent-host daemon shares process-wide infrastructure
while per-agent model/runtime caches remain bounded.

- **Agent host** runs local agents as asyncio tasks with a concurrency limit.
  An idle identity has no active task. Model objects may remain in the bounded
  cache; eviction does not delete identity or checkpoint state. Disposable
  execution children and persistent PTY shells have separate resource costs.
  There is no runner-mode toggle or individual agent process to maintain.
- **Heartbeat owns the idle nudge, hibernation is gone** — the heartbeat's own
  idle threshold (`AVA_HEARTBEAT_IDLE_THRESHOLD_SECONDS`, default 300s) nudges
  idling agents; agents that paused their own heartbeat
  (`ava.self.pause_heartbeat`) simply stop being nudged.
  [`decisions/2026-06-22-heartbeat-opt-out-over-escalation.md`](../decisions/2026-06-22-heartbeat-opt-out-over-escalation.md).
- **Per-cluster data plane is sized to be noise, not a multiplier** — every
  cluster (including each dev worktree) runs its own Postgres + Redis instance
  for isolation, but each instance costs only ~100-150MB RAM (`shared_buffers`
  tuned down + Redis ~5MB) — roughly one agent's own resident cost, not a
  per-cluster tax that compounds with fleet size.
  [`future/infra/embedded-per-cluster-data-plane.md`](../future/infra/embedded-per-cluster-data-plane.md).
- **Shared browser, not one Chrome per agent** — the `browser` /
  `browser-mcp` services above own ONE headed Chrome + ONE
  `chrome-devtools-mcp` upstream, multiplexed to every agent over a Unix
  socket, instead of spawning a browser (and its CDP collector buffers) per
  agent that touches `ava.mcps.chrome.*`.
- **Lazy MCP connections** — the per-agent MCP daemon subprocess boots at
  agent start (its cold start is overlapped with the rest of boot — see
  warm-up above), but it does not eagerly connect to every configured MCP
  server: each server connection opens only on that tool's first call, and the
  tool schema itself is cached on disk for 24h so repeat discovery costs no
  round trip. [`okf/mcps/mcps.ava.okf.md`](../okf/mcps/mcps.ava.okf.md).
- **Fixed, small per-agent connection budget** — 2 pooled Postgres
  connections (shared with the LangGraph checkpoint saver) + one Redis
  subscription per agent, with pgbouncer transaction pooling in front of the
  cluster's Postgres so the connection count does not scale 1:1 with fleet
  size (hosted mode replaces per-agent pools with one bounded workload pool and
  one fixed four-connection control pool for the runner).
  [`agent/db/docs/db.ava.okf.md`](../agent/db/docs/db.ava.okf.md).

None of this claims memory stops mattering — it is the honest current floor.
The next walls once memory is handled: the heartbeat's
wake-rate ceiling (~1.67/s, ≈750 agents on today's numbers) and LLM turn cost,
which is linear in fleet size regardless of any of the above.

### Post-deploy visual gate

On the macmini runtime host, export `AVA_VISUAL_GATE_COOKIE_FILE` as a 0600
Playwright storage-state JSON, Netscape cookie jar, or single `name=value` file,
then run `scripts/post_deploy_visual/check.py --check --base-url <production-gate>
--health-url <gateway-origin>` (the gate serves the SPA wall for
unauthenticated /api, so the health probe must target the gateway origin
explicitly; the script appends `/api/health`).
The wrapper calls the public gateway health API to compare process `started_at`,
runs the browser pass with the repo-pinned Playwright Chromium headless on the
host (no Docker), and writes `probes.json`, `meta.json`, and capture artifacts
beneath
`~/post-deploy-visual/<wave-sha>/`. It never routes notifications: the invoking
agent sends a P0 result to #3242 and #405 with `send_message`, or queues P2 with
`notify`. It runs after rollout and cannot block deployment. Exit 20 is P0,
exit 10 is P2, and exit 0 is green or expected drift.
The daily 07:30 invocation and a same-process-start run are sentinels and do not
advance the two-deployment-wave escalation counter. A concrete first-wave
invocation: `scripts/post_deploy_visual/check.py --check --base-url
<gate-entry-url> --health-url <gateway-origin-url>` — the base URL is the
gate (frontend entry), never the gateway API origin, and the script refuses a
base URL that answers the gateway health JSON up front.

No command updates a golden implicitly. After a reviewer or #405 confirms a report,
roll it forward with `scripts/post_deploy_visual/check.py --accept-wave <sha>
--accepted-by <reviewer>`; this appends the reviewer, UTC timestamp, SHA, and
capture list to the 0600 `acceptance-audit.jsonl`. If the exported cookie leaks,
revoke it immediately with `curl --fail-with-body -X POST --cookie
"$AVA_VISUAL_GATE_COOKIE_FILE" "$AVA_VISUAL_GATE_URL/api/auth/logout"`, delete
the leaked file, export a fresh session cookie, and restore mode 0600 before the
next run. The curl revocation consumes Netscape and `name=value` cookie files;
a Playwright storage-state JSON export must be revoked from the logged-in UI.

### Start / check / restart

The CLI owns local lifecycle; `ava start` initializes a fresh home and resumes an
existing one through the same path. First-start inputs persist before native
effects. Repeated start checks the same identity and selected service roster.

```bash
ava start
ava status
ava pause
ava stop -y
ava restart
ava cluster status
ava cluster destroy [--drop-db]
```

`pause` retains infrastructure, browser and persistent PTYs. Full `stop` closes
those resources; `--keep-infra` and repeated `--keep-service` preserve explicitly
selected resources. `restart` is the ordinary local pause/start path. Destroy
decommissions this host's cluster: it stops it, retires the host's native jobs
(launchd, crontab, the Linux boot unit, the permissions helper) and marks the home
detached, so `ava start` refuses it until `destroy-intent.json` is deleted by hand;
`--drop-db` additionally removes the data directories (`pg/`, `redis/`). It acts on
this process's home, `~/.ava` included, and asks you to type the home path at a
terminal: with no terminal on stdin and stdout it refuses, and no flag skips the
prompt (over ssh, use `ssh -t`). A home that is not the default home neither
registers nor removes OS jobs, because their names carry no home.

A runner joins through `ava init`. Supply the capability
bundle's `AVA_DB_CAPABILITY_KEY` without echoing it, then use its checkout's
`.venv/bin/ava init --serve-agent-runner --no-serve-gateway --gateway-url URL
--machine-name NAME --machine-host HOST --db-capability BUNDLE` and `ava start`
(`ava` on PATH does not exist until the first start; the bundle comes from `ava
cluster db-authority issue-unit` on the gateway). Bootstrap publishes no database
credential and not the human secret; the runner's login, API token and
telemetry token arrive only in that unit's bundle (they are shared by every
runner unit of the generation), and a runner never holds
`AVA_CLUSTER_SECRET` (a home that still records it refuses to start). Memory checkout initialization remains
explicit through `ava memory init`.

**Health observations.** `ava cluster health-probe` retries
gateway liveness three times, 30 seconds apart, before declaring it unhealthy.
It checks data-volume usage before gateway liveness so a full disk that prevents
gateway startup is reported as disk pressure. Both the crash-loop and schema
checks are enabled by default; `--no-crash-loop-check` and `--no-schema-check`
disable them individually.
Gateway and population failures retain their code, environment, or local-maintenance
classification. A disabled agent-host or native maintenance hold cannot hide a low
global population: the probe still exits 1 and grades that outage. A live cluster
deploy can pause explained alert grading while retaining the episode's true start;
disk pressure remains independent.

The OS job and CLI probe only observe and alert. They do not invoke rollback,
maintain release-policy failure counters, or promote pending code to known-good.
`--auto-rollback` and rollback `--threshold` options are rejected, including at
health-probe registration. Registration replaces this home's scheduled payload with
`ava cluster health-probe`; other homes' jobs stay untouched.

A dev/QA cluster's birth seeds `AVA_HEALTH_PROBE_AGENT_MIN=0` when it has no resident
agents by design; an explicit value stands as written. Crash-loop limits remain
health observation parameters.

**`ava start` reports the admitted roster's readiness.**

| rc | Meaning | What to do |
|---|---|---|
| `0` | All selected services passed fresh ownership-bound protocol probes, and any Linux systemd handoff adopted the same native root | Inspect status for later health changes |
| `4` | Launch failed or at least one selected service did not become ready within its deadline | Read the named failures; retrying the same start reuses the same generation |
| `1` | A lifecycle step or native manager handoff failed | Read the step failure; unknown custody remains retained |

The roster follows host capabilities and persisted service selection. Frontend
is included. Missing probes and unobservable ownership cannot pass readiness.
Critical services keep the full startup deadline; other selected services have a
shorter deadline, but either failure keeps start incomplete. There is no readiness
waiver for boot or rollout. Root owns application health recovery; retries do not
create another service supervisor. Changing the immutable root's service inputs
requires a normal stop before start.

The Linux boot unit runs ordinary start directly. `Type=forking` and a
private, birth-validated PIDFile let systemd adopt root after the ready starter exits;
`TimeoutStartSec=900` bounds only startup. `KillMode=process` signals root alone,
so the independent data plane survives while root closes its own application
tree. Failed closure retains custody; systemd cannot prove orphan closure after
abrupt root death. There is no convergence shell script or duplicate proxy probe.

**OS-scheduled jobs.** The platform scheduler runs the health probe
(`base/host/system/cron.py`), boot autostart (`base/host/system/autostart.py`),
daily rotate-then-retain log maintenance (`base/host/system/logs_job.py`), and the
per-machine content-refresh pass (`base/host/system/packages_job.py`)
— as launchd LaunchAgents on macOS and systemd boot plus scheduled maintenance on
Linux. Automatic startup requires systemd. Converge registers and enables the native
home unit without starting a recursive caller; ordinary interactive start may
launch root directly. The unit carries the exact home, checkout and registry.
There is no cron boot route or second boot-install command. Two properties are
load-bearing:

- **A job spec is anchored to the checkout that wrote it.** `ava_binary_path()`
  resolves this checkout's `.venv` binary (PATH only as a fallback), and the
  launchd plist / systemd unit / maintenance crontab line pin `AVA_HOME` explicitly, so a job registered by
  cluster X can never run cluster Y's `ava` against cluster Y's home.
- **A scheduled process never reloads its own launchd label.** `launchctl bootout`
  would terminate the registering process tree. Registration defers on the
  direct child's label match (the env fast path) or on a proven live-process-
  tree match (`launchctl print` pid + `ps` ancestry walk) — the inherited
  `XPC_SERVICE_NAME` alone is not proof for descendants, which read "0"
  (`postmortems/0008`) — leaves the existing plist untouched, and lets the
  next external converge apply any pending spec change.
- **`AVA_OS_JOBS_ENABLED=false` disables registration for a process.** The
  scheduler is one namespace per OS user, so a test-scoped `$AVA_HOME` cannot
  isolate it — the pytest suite sets this and `tests/fixtures/provisioning.py` fails any run
  that leaves a job behind. Deregistration is never gated. Operators do not set
  this: a prod cluster with it off silently loses its health probe,
  daily log maintenance, and its ability to come back after a reboot.

`ava pause`, `ava stop`, restart and update use the native maintenance primitives.
Pause retains infrastructure and persistent PTYs; default stop closes those local
resources. Durable agent identity and work remain on disk.
A stop timeout is a failure; force escalation requires an explicit option.
Normal `ava start` resumes only after readiness. See the
[pause/stop procedure](graceful-maintenance.md) for partial stop, coordinated
multi-machine ordering, failure recovery and the first-deployment limitation.

Stop-class drills and operations: see the executor-cancellation insurance and hold handover section of [graceful maintenance](graceful-maintenance.md).

Merging a PR does not deploy it. `ava stop` asks for confirmation unless `-y` is supplied.


### Updating a networked cluster in source mode

A cluster whose units span machines, each running from `$HOME/.ava/source`, is
updated by stopping every unit, switching every checkout and starting again
([decision](../decisions/2026-09-30-networked-cluster-stays-on-source-updates.md)).
`python -m cli.fleet_update` runs it from the operator's development checkout
over key-based SSH, in two halves; any manual step the release needs goes in
between. When the operator's Mac is itself a host, it must be able to `ssh` to
itself (its own public key in its own `authorized_keys`):

```bash
.venv/bin/python -m cli.fleet_update down --new NEW_SHA \
  --gateway GATEWAY --runner RUNNER [--runner ...] --log-dir DIR
# manual release steps, if any
.venv/bin/python -m cli.fleet_update up --gateway GATEWAY --runner RUNNER [--runner ...] --log-dir DIR
```

- `down` refuses (exit 2, nothing changed) when a checkout has a
  `post-checkout` hook, uncommitted changes or `$HOME/.ava/updates/active`,
  when a host holds maintenance other than a completed stop, when the hosts
  disagree on HEAD, when `.python-version` changes without
  `--allow-python-change`, or when it runs inside an Ava agent's shell. It
  prints `git diff --stat OLD..NEW` over migrations, helper sources and locks,
  fetches NEW on every host, runs `ava stop -y --timeout 600` on each runner and
  then the gateway (each must leave phase `stopped` with no failures), then on
  every host checks out NEW detached, repairs legacy read-only venv
  directories, runs `uv sync --frozen` and requires a clean tree.
- Runners fetch through the gateway's source clone (their checkout's `origin` is the gateway host's `~/.ava/source`): the target commit is fetched by SHA before it is reachable from any of the clone's refs, so the clone carries `uploadpack.allowAnySHA1InWant=true` (repo-local; set 2026-09-30). A reclone of the clone must re-apply it.
- A failed stop prints the next step: on that host retry
  `ava stop -y --timeout 600`, confirm `ava maintenance status` shows
  paused/stopped, then rerun `down`. A tree that is not clean after the switch
  prints its `git status --porcelain` (first 20 lines). Untracked files are not
  caused by the update (a changed `.gitignore` can reveal them): check them and
  move them away, never delete, then rerun `down`. Nothing is moved, deleted or
  retried automatically.
- `up` runs `ava start` on the gateway (its cold start applies migrations),
  then on each runner; on macOS as a one-time LaunchAgent in `gui/<uid>`,
  because signing the helper needs the login keychain (the user must be
  logged in to the GUI). Each start must release its hold; every machine
  listed by `--gateway`/`--runner` must be online on the roster with checkout and
  running code on one commit (`online` follows the heartbeat, so the roster is
  re-read every 5 seconds for up to `--roster-timeout`, default 90, and a failure
  shows the last read). A listed host is matched to its roster row by the machine
  name it reports itself (`machine_name()`), not by its SSH alias; a name with no
  row fails the half. Roster machines that are not listed (a laptop that is off) are
  reported with their online flag and commits and never fail `up` (see the last bullet for their old processes). Then
  the gateway smoke-tests each listed agent-runner with a real agent, reading its own
  address and bearer in place; last, each host runs `ava packages refresh`
  (skills follow their channel; `ava skill update` is retired) and its summary
  line (`applied N, conflict M`) is printed. Conflicts are only reported: the
  script never passes `--force`, which is human-only.
- The first failure stops a half and nothing rolls back: fix the cause and
  rerun the whole half, which is idempotent. `--dry-run` runs only the
  read-only checks and prints the effects. Output is redacted and tee'd to
  `DIR`.
- **A manual step that rewrites a state file comes before any command of the new
  code.** When a release's manual steps change a file the code reads (a key dropped
  from `start-intent.json`, a renamed file), finish them on every host already switched
  to NEW before that host runs any NEW command, `ava stop` included. A rerun of `down`
  is such a command: it runs `ava stop` again on a host already on NEW, and NEW refuses
  the state file that is not yet converted. Rerun `down` only after the step is done on
  the hosts it has switched.
- A unit `down` could not reach (a laptop offline) keeps running its old
  processes. The gateway's start in `up` raises the cluster's minimum code
  version, and those processes exit when they next touch the database
  ([code version gate](#code-version-gate)).

### Release steps: adding the `ttl-reaper` and `schedule-manager` port slots (one-time)

The release that moves the TTL reaper and the schedule manager out of the gateway
process into their own `ttl-reaper` and `schedule-manager` services adds two slots to
the fixed port table (`ttl_reaper` 8121, `schedule_manager` 8122), so every gateway
home's start intent needs those keys before any command of the new code (see the rule
above). A runner-only home has no reservation and needs nothing; a home's `.env` needs
nothing either, the unset `AVA_<NAME>_HEALTH_PORT` binds the table's number. The step is
idempotent (it adds a key only when absent), so a home that already ran it, or that ran
the PITR step below in the same window, is unchanged by a second run.

1. **Between `down` and `up`, on every gateway home**, add the slots. The file is
   compact JSON with sorted keys, mode 0600:

   ```bash
   python3 - <<'EOF'
   import json, os, pathlib
   home = pathlib.Path(os.environ.get("AVA_HOME") or pathlib.Path.home() / ".ava")
   path = home / "start-intent.json"
   data = json.loads(path.read_text())
   for slot, port in (("ttl_reaper", 8121), ("schedule_manager", 8122)):
       data["record"]["ports"].setdefault(slot, port)
   staged = path.with_name(path.name + ".staged")
   staged.write_text(json.dumps(data, sort_keys=True) + "\n")
   staged.chmod(0o600)
   staged.replace(path)
   EOF
   ```
2. **After `up`**, the migrations (`maintenance_state`, the schedule-manager columns and
   `schedule_sync_requests`) have been applied by the gateway's cold start and the units
   are running: `ava status` lists `ttl-reaper` and `schedule-manager` ready, and
   `curl -s localhost:8121/healthz` / `localhost:8122/healthz` answer with their names and
   all loops alive. The gateway no longer runs a reaper or a schedule manager of its own.
   Schedule sessions are not restarted by the rollout: the new service re-adopts the live
   ones (a gateway-less window only delays launches and sync requests).

### Release steps: retiring the PITR stack (one-time)

The release that deletes the self-written PITR stack
([decision](../decisions/2026-10-02-delete-the-self-written-pitr-stack.md)) needs
three manual steps per gateway home, because the upgrade cannot do them itself.
The last commit that still carries the stack is the tag `pitr-stack-final`. The
daily dump, its off-site publish and the weekly restore drill import none of the
deleted code.

1. **Before `down`, with the old code running.**
   - `SHOW archive_mode;` must read `off` and `SHOW archive_command;` must be
     empty (`pg_stat_archiver.archived_count` zero). A home that reads `on`
     first resets `archive_mode`, `archive_command`, `archive_timeout` and
     `wal_compression` with `ALTER SYSTEM RESET` and restarts Postgres: the
     uploader that drained the archive spool is gone, and a spool at its hard
     bound makes `pg_wal` grow until the disk is full.
   - Record the off-site destination: `AVA_PITR_OSS_ENDPOINT` and
     `AVA_PITR_OSS_BUCKET` from `ava config get`, and the path in
     `AVA_PITR_OSS_CREDENTIALS_FILE` (masked there; it is the 0600 file the old
     configuration pointed at).
   - Unset every writable retired key while the old code still knows it; after
     the upgrade official config can no longer touch it:

     ```bash
     ava config unset AVA_PITR_ENABLED AVA_PITR_BASE_BACKUP_ENABLED \
       AVA_PITR_RESTORE_PROOF_ENABLED AVA_PITR_RETENTION_PLANNER_ENABLED \
       AVA_PITR_RETENTION_DELETE_ARMED AVA_PITR_RETENTION_DELETE_APPROVED_DIGEST \
       AVA_PITR_STORE_BACKEND AVA_PITR_BAIDU_TOKEN_FILE AVA_PITR_OSS_ENDPOINT \
       AVA_PITR_OSS_BUCKET AVA_PITR_OSS_CREDENTIALS_FILE \
       AVA_PITR_OSS_VIEWER_CREDENTIALS_FILE AVA_PITR_OSS_DELETE_CREDENTIALS_FILE
     ```

     The off-site leg of the daily dump is idle from here until step 3; the
     local dump is unaffected. The other `AVA_PITR_*` keys are deploy-provisioned
     and read-only (`ava config` refuses them). They stay in the `.env`, inert:
     every settings model ignores a key it does not declare. Do not edit the
     `.env` by hand to remove them.
2. **Between `down` and `up`, on every gateway home** (a runner-only home has no
   reservation and needs nothing): delete the four retired port slots from the
   start intent in this one step, the two PITR slots (`pitr_uploader`,
   `pitr_base_backup`) and the two watchdog slots (`gateway_watchdog` 8119,
   `agent_runner_watchdog` 8120; no daemon binds or reads them). New code
   refuses a reservation whose slots differ from the fixed port table, so this
   is a state-file rewrite that comes before any command of the new code (see
   the rule above). The file is compact JSON with sorted keys, mode 0600:

   ```bash
   python3 - <<'EOF'
   import json, os, pathlib
   home = pathlib.Path(os.environ.get("AVA_HOME") or pathlib.Path.home() / ".ava")
   path = home / "start-intent.json"
   data = json.loads(path.read_text())
   for slot in ("pitr_uploader", "pitr_base_backup", "gateway_watchdog", "agent_runner_watchdog"):
       data["record"]["ports"].pop(slot, None)
   staged = path.with_name(path.name + ".staged")
   staged.write_text(json.dumps(data, sort_keys=True) + "\n")
   staged.chmod(0o600)
   staged.replace(path)
   EOF
   ```
3. **After `up`.** Set the off-site destination under its new names, then apply
   the restart hint `ava config set` prints so `pg-backup` reads them:

   ```bash
   ava config set AVA_BACKUP_OFFSITE_ENDPOINT=<endpoint> AVA_BACKUP_OFFSITE_BUCKET=<bucket> \
     AVA_BACKUP_OFFSITE_CREDENTIALS_FILE=<credentials file>
   ```

   Judge the next dump by its destination, not by silence: `ava-logical/<that
   dump's file name>` exists in the bucket with the size of the local
   `.dump.enc`, and the log carries `[backup] off-site published`. Also check
   `ava backup operations status` (all ready), `pg_backup` health, and that
   `$AVA_HOME/run/ava-root/manifests.json` no longer lists `pitr-*` services.

Everything else the stack left is inert and removed only with separate
approval: `$AVA_HOME/physical-backup/` and `$AVA_HOME/runtime/pg-archive/`
on disk, a `REPLICATION` role and the `replication` rows it needed in
`pg_hba.conf` (rewritten on every start, so the rows disappear by themselves),
and the remote objects and lifecycle rule of the retired physical chain.

### WAL-G archiving

WAL archiving ships every completed WAL segment, encrypted, to an OSS prefix
through a pinned WAL-G
([decision](../decisions/2026-10-02-walg-physical-backup.md),
[node](../services/gateway_side/walg/docs/walg.ava.okf.md)). It is off until
`AVA_WALG_CONFIG_FILE` is set; unset, nothing of it runs. It archives WAL and, once a
day, takes a base backup, verifies the archived chain and applies retention; there is
no restore yet, so it is not a recovery path.

**Files the operator places** (0600, owned by the gateway's OS user):

- The WAL-G configuration JSON, in WAL-G's own format; every key below is required:

  ```json
  {
    "WALG_OSS_PREFIX": "oss://<bucket>/ava-walg/<home label>/pg17/gen1/",
    "OSS_ACCESS_KEY_ID": "...",
    "OSS_ACCESS_KEY_SECRET": "...",
    "OSS_ENDPOINT": "https://oss-cn-shanghai.aliyuncs.com",
    "OSS_REGION": "cn-shanghai",
    "WALG_LIBSODIUM_KEY_PATH": "<path of the key file>",
    "WALG_LIBSODIUM_KEY_TRANSFORM": "hex",
    "WALG_PREVENT_WAL_OVERWRITE": "true"
  }
  ```

  `OSS_REGION` is the bare region id (`cn-shanghai`), not the endpoint's `oss-` form:
  OSS rejects the signature with "Invalid signing region" otherwise, and the
  configuration check refuses an `oss-` value. The prefix names a path (the PG major and a generation number belong in it) and
  cannot sit under `ava-logical/` (the daily dump), `ava-pitr-scratch/` or
  `ava-wsl-cutover-*`. Use a dedicated storage account whose policy is limited to that
  prefix with Get, Put, List and Delete (plus AbortMultipartUpload and ListParts):
  retention deletes, and the logical-dump upload credential must not be able to touch
  the physical chain.
- The libsodium key, 32 random bytes in hex: `umask 077; openssl rand -hex 32 > <key
  file>`. **Losing it makes every archived segment unreadable.** Copy it to two places
  off the gateway host and decrypt-verify a copy before enabling. There is no rotation:
  a new key means a new prefix (a new `gen`).
- A bucket lifecycle rule must not expire objects under the WAL-G prefix: two independent
  deleters break the chain (the daily tick's retention is the only one). Keep any bucket-wide expiry scoped to the other prefixes. Cleaning up abandoned
  multipart uploads can stay bucket-wide.

**Switching it on.** `ava backup walg check` proves the binary, the configuration, the
key and the storage permissions (it writes and deletes one small object under the
prefix). Then:

```bash
ava config set AVA_WALG_CONFIG_FILE=<path of the JSON>
ava stop && ava start          # archive_mode is read only when Postgres is launched
ava backup walg status         # archive_mode=on, failing_now=False, health: ok
ava backup walg run            # the first base backup, supervised (see "The daily tick")
```

Converge installs the pinned binary (`$AVA_HOME/runtime/walg/wal-g`; Linux x86_64 only,
anything else fails here), validates the configuration and records the key fingerprint
in `$AVA_HOME/backups/walg/key-id` before Postgres starts, so a bad configuration fails
`ava start` instead of producing a Postgres whose archive command can never succeed. A
plain update retains the running Postgres and does not activate or deactivate
archiving: `ava start` prints a warning when the running Postgres differs, and the
health probe reports it. After a start, `SELECT pg_switch_wal();` and an increase of
`archived_count` in `pg_stat_archiver` show a segment arriving in the bucket.

**The daily tick.** While the key is set, converge keeps one OS job registered
(crontab line `# ava-walg` on Linux, LaunchAgent `com.ava.walg` on macOS; default home
only) that runs `ava backup walg run` once a day, three hours after the daily dump
becomes due (`AVA_BACKUP_HOUR`), writing to `$AVA_HOME/logs/walg.log`. The command is safe
to run by hand at any time and to repeat: a second run while one is going stands down
(exit 0), and a run during a deploy window or while Postgres does not accept
connections is recorded as skipped (exit 0), not failed. A run is: preflight (binary,
configuration, key pin, one `backup-list`), `backup-push` with `WALG_DELTA_MAX_STEPS=6`
(WAL-G makes a full backup when the increment chain is six deep, so a daily run makes a
full backup every seventh time, and a failed full backup is retried the next day), `wal-verify
integrity timeline --json` (only the JSON status counts; WAL-G exits 0 while it reports a
gap, and `WARNING` is archiving in flight, not a failure), then retention. The first
failing step ends the run and is recorded; the next day starts over. Run the first full
backup by hand and watch it: its duration and upload volume are not yet measured on this
bucket.

Retention is `delete retain FULL 3 --use-sentinel-time`, run first without `--confirm`.
The objects WAL-G lists are written to the log, then three invariants must hold or the
run fails at `retention` with nothing deleted: every key is under `basebackups_005/` or
`wal_005/`; no key belongs to the newest backup or a backup it is an increment of; at
least one full backup survives. Nothing is asked of WAL-G until more than three full
backups exist. `wal-g delete garbage` is not used: the orphan files a failed full backup
leaves cost only storage.

`ava backup walg status` shows the last tick, run, backup, verification, retention and
recovery drill from `$AVA_HOME/backups/walg/state.json` (0600; the tick writes it, the
health probe reads it; remove it only to reset a state the tick reports as unreadable).

**The weekly recovery drill.** A backup nobody has restored is not a recovery path, so the
tick restores one every week, before that day's backup (it therefore restores yesterday's:
the one a lost host would need). The drill is due one tick period before a week since the
last success, and a failed drill is retried by the next tick. It fetches the newest backup
into a scratch directory (`AVA_PG_THROWAWAY_BASE`, else the platform default, else the disk
fallback; room is the backup's uncompressed size plus the WAL since it started plus
`max_wal_size`, and a base that cannot hold that is refused), recovers it with a scratch
postmaster to the start of the newest archived WAL segment, and reads a real conversation
back through the checkpoint reader. Reaching that point proves every segment between the
backup and it is in the bucket, decrypts and replays: a missing one ends recovery in
Postgres' own FATAL, which the record carries (`ava backup walg status`, "last drill").
The scratch copy and its process are removed in every outcome, and a failed drill never
stops the backup that follows. `ava backup walg drill` runs it now (same lock as the tick);
after switching WAL-G on, run it by hand once after the first `run` and check the result
before trusting the schedule. Its download saturates the downlink for roughly the length of
a base-backup fetch (an estimate, not measured on this bucket): schedule hand runs
accordingly.

**Restoring** (`ava backup walg restore --dir <empty directory> [--backup NAME]
[--user <superuser>] [--time 'YYYY-MM-DD HH:MM:SS+00' | --lsn X/X]`). It never touches this home's data
directory (a `--dir` that is or contains `$AVA_HOME/pg` is refused) or its ports. Steps:

1. Pick the backup. `LATEST` is the default; to recover to a time or LSN, name the newest
   backup that *started* before it (`wal-g backup-list --detail` shows start times; an
   increment resolves its whole chain by name). `LATEST` can name a backup newer than the
   target, which cannot be recovered to it.
2. Run the verb on a host with the same Postgres major (17), the same extensions
   (pgvector), the configuration JSON and a copy of the encryption key. Without `--time`
   or `--lsn` it recovers to the end of the archive. It fetches the backup, writes
   `recovery.signal`, and starts a scratch postmaster on that directory (unix socket only,
   its own temporary socket directory and port, `archive_mode=off`, the capacity settings
   `pg_controldata` records, `restore_command` = `wal-g wal-fetch %f %p`) until it is
   promoted, then shuts it down cleanly. The directory is left as a promoted database on a
   new timeline; it is not started.
   `--user` names the restored cluster's superuser, the OS user that ran initdb on the
   source (default: the current OS user); a role Postgres refuses ends the restore at once.
3. A missing segment, a wrong key or an unreachable target ends in Postgres' FATAL, printed
   with the end of its log; the directory is left for inspection, and must be emptied
   before a retry.
4. Before this database replaces the live one or backs a new primary: **use a new WAL-G
   prefix (a new "generation", e.g. `.../gen2/`) and take a new full backup first.** The
   recovered database is on a new timeline; archiving it into the old prefix would mix two
   histories there.

Scope: this recovers the **database** under the same home identity. A whole-host rebuild
(the `$AVA_HOME/db-authority/` ledger, `.env`, `start-intent.json`) is not covered or
exercised; see `conventions/disaster-recovery.md`.

**Switching it off.** `ava config unset AVA_WALG_CONFIG_FILE`, then `ava stop && ava
start`; converge removes the daily job. Nothing is left in the data directory (the archive
settings are launch arguments). The objects stay in the bucket; deleting them is a manual storage action.
There is no way to stop archiving without a restart: when archiving misbehaves, repair
its cause (credentials, network, disk).

**What the health probe reports** (alert-only, graded like disk usage; the texts carry
no number so one outage is one episode): the running Postgres does not carry the
configured archive settings (restart pending); the archiver is failing; complete WAL has
waited in `archive_status/` for longer than the 300 s RPO objective (this is also how a
hung archive command shows, since it neither succeeds nor fails); the key file is not the
pinned key (restore the pinned key or start a new prefix; never replace the key under an
existing prefix). From the daily tick's state file: the last executed run failed (one
text per step: preflight, backup, verification, retention; read `ava backup walg status`
and the end of `walg.log`); the last verification saw a broken chain (`FAILURE`; a missing
segment cannot be repaired, so take a new full backup and treat the older recovery points
as lost); no tick has started within 24 hours (the job is not registered or not running;
a deliberate skip counts as started, and before the first tick the age of the key pin
stands in); the latest recovery drill failed (read "last drill" in `ava backup walg
status`: the detail is Postgres' own error or the content check that failed); no drill has
succeeded within a week plus a tick (the same age stands in before the first drill). A
failed run or drill stays reported until a later one succeeds; skipped ticks do not
clear it. The probe reads the queue over the admin socket because the application login
may not list `archive_status/`.

**Before a planned stop**, look at `ava backup walg status`: Postgres' shutdown waits for
the archiver to finish its queue, so a backlog (or a hung `wal-g`) delays `ava stop`. A
fast shutdown still unfinished near the end of the stop's budget is ended by an immediate
shutdown and the leftover `wal-g` processes are killed; the stop reports it (an error
log, the `postgres_stop_escalated` event, an `escalations` entry in the stop journal) and
completes. The next start replays WAL and the archiver ships what was left in `pg_wal`.

### Agent recovery after a provider billing stoppage

A provider balance exhaustion (e.g. DeepSeek HTTP 402) classifies as a
permanent rejection; two consecutive ones open the recovery breaker
(`permanent_reject_streak >= 2`, reason `billing`) and the corpse reaper
terminates the victims. Recovery after a top-up is one explicit command:

```bash
ava agents resurrect-billing            # strictly read-only preview
ava agents resurrect-billing --execute  # balance gate -> resurrect each candidate on its home machine
```

The preview itself is the identification tool: it lists the billing-class halt
victims (terminated, streak at the halt threshold, reason `billing`, and not
`user` / `integrity`-terminated), the halted-but-alive rows
(report-only — no action is needed; the halt clears on their next successful
turn), and the live provider balance readout. `--execute` refuses (exit 1)
unless the balance endpoint reports the account available above
`AVA_BILLING_RECOVERY_MIN_BALANCE` (default 1.0); a probe failure refuses too —
fix the account or the `AVA_BILLING_RECOVERY_*` config and rerun. The run is
idempotent: the candidate set self-clears, a rerun is an audited no-op, and a
concurrent second run is refused by the run-level single-flight lock.

Boundaries: `user` / `integrity`-terminated rows and halted-but-alive agents
are never actioned — the latter recover on their next inbound; the former stay
a per-agent human decision (`ava agents resurrect <id>`). Audit lands on the `billing_resurrect` event
(balance readout + candidate / resurrected / refused / deferred / failed sets)
plus `billing_resurrect_run` telemetry; each resurrected agent's own
`resurrect` event carries `via='billing_recovery'`. Rationale and rejected
alternatives: [billing batch resurrect decision](../decisions/2026-09-18-billing-batch-resurrect.md).


## Private-network deployment (phone / multi-device access)

The gateway binds all interfaces on the **gateway host** (both address
families); the Next.js app binds loopback only and is reachable **only through
the fleet UI gate** — the root-owned entry on `:3000`, which itself binds all
interfaces and proxies the app (`services/gate`). Any private-network device
(laptop, phone, other agent-runners) hits them directly at the gateway's
private-network address — gateway on `:8000`, UI entry on `:3000`. The exact
host is whichever node holds the gateway role (a single-box deployment's only
host). The access model below is the authoritative description of ports and
trust boundary.

### Access model — private-network reachability + always-on cluster-secret auth

The cluster runs entirely on one private network — gateway,
agent-runners, and the user's own devices (laptop, phone) are **one trust
group**. The gateway is reachable **only** over the private network — there is
no public ingress, the gateway host has no public IP (and the earlier
Cloudflare Tunnel was retired) — but reachability is not trust: every
authenticated route requires a bearer or a session cookie (browser login). A
human or operator presents the cluster secret (`AVA_CLUSTER_SECRET`, gateway
only); services, agents and remote units present their write generation's
machine API token (`AVA_API_TOKEN`: the gateway admits the active
generation's, a unit's `/ops` its own generation's). See
[`decisions/2026-06-11-multihost-deployment.md`](../decisions/2026-06-11-multihost-deployment.md)
(explicitly flags its own §4/§5/§9 "no auth" description as superseded history)
and [`Credential rotation`](#credential-rotation) below for the bearer and
data-plane procedures. The user opens the UI / API at the gateway's private-network
address (on a VPN overlay, prefer its DNS name over a raw
`100.x`-style IP where the overlay offers one — IPv6-only carrier networks
NAT64-synthesize IPv4 literals and the request never enters the tunnel):

- `http://<gateway-host>:8000` — gateway (API + SSE)
- `http://<gateway-host>:3000` — frontend

The frontend resolves the gateway as `${location.hostname}:8000` (frontend
`:3000` and gateway `:8000` are co-located on the gateway host, different
ports). Gateway CORS accepts exact origins only. An empty
`AVA_GATEWAY_CORS_ALLOWED_ORIGINS` derives localhost, `127.0.0.1`, and the
configured gateway host at the frontend entry port, plus localhost and
`127.0.0.1` at the cluster's reserved app port (`AVA_APP_PORT`, loopback-only
like the Next.js bind); set the variable to a comma-separated list to replace
that derived allowlist. Cookie-authenticated
state changes also reject a present, non-allowlisted `Origin`. On a host where
another service holds the gateway port, set
`AVA_GATEWAY_PORT` plus the matching
`AVA_GATEWAY_HEALTH_URL=http://localhost:<port>/api/health`
(two-var contract; `ava status` probes the health URL). A pure agent-runner
derives this health URL from the reachable `AVA_GATEWAY_URL` written by
runner first start unless the host sets an explicit health URL override.

### Transport encryption

A secret-bearing cluster that serves the gateway or ops server on a non-loopback
address MUST declare `AVA_TRANSPORT_ENCRYPTION`; every start checks the
declaration and refuses to start when it is empty or unsupported. The accepted
modes are:

- `tls` — TLS terminates at an endpoint immediately in front of each gateway or
  ops listener. The built-in listeners receive the decrypted connection; this
  declaration records the deployment boundary and does not configure certificates.
- `mtls` — mutual TLS authenticates both ends of each protected connection before
  traffic reaches the gateway or ops listener.
- `overlay` — an encrypted private overlay network carries the complete path
  between the gateway, agent-runners, and client devices.

Set the declaration in the cluster configuration before exposing a secret-bearing
listener. An empty declaration is valid only while every listener remains on
loopback.

## Credential rotation

`AVA_CLUSTER_SECRET` is the gateway's human bearer (API, frontend login); no
remote unit holds it, and machine API tokens rotate with every write
generation. Run
[`scripts/data_plane_ops/rotate_cluster_secret.py`](../scripts/data_plane_ops/rotate_cluster_secret.py)
(`--execute`) only after a bearer leak. Rotating the secret never touches the
logical-backup passphrase `$AVA_HOME/backups/logical-backup.passphrase`: a gateway
home's birth mints and pins it, independent of the secret; a home born earlier carries
`sha256(secret)`, pinned once, so every earlier logical backup keeps decrypting (an
empty-secret home carries a minted one instead). The script verifies the pin before it
writes the new secret, and journals each step with fingerprints, never secrets.
**The pinned file is backup-critical material**: nothing re-derives it, and losing it
makes every logical backup of the home unreadable; escrow a copy with the gateway's
backup keys ([decision](../decisions/2026-09-28-backup-passphrase-minted-at-birth.md),
[passphrase](../services/gateway_side/backup/docs/passphrase.ava.okf.md)). Restart the
gateway, then issue every remote unit a new capability bundle (its telemetry token
derives from the secret). It does not change Postgres, Redis, ACLs, or PgBouncer.

Routine data-plane rotation is independent and uses
[`scripts/data_plane_ops/rotate_data_plane_secrets.py`](../scripts/data_plane_ops/rotate_data_plane_secrets.py):

```bash
.venv/bin/python scripts/data_plane_ops/rotate_data_plane_secrets.py                 # dry-run, both scopes
.venv/bin/python scripts/data_plane_ops/rotate_data_plane_secrets.py --scope admin --execute
.venv/bin/python scripts/data_plane_ops/rotate_data_plane_secrets.py --scope runner --execute
```

Run this script in a gateway context, not an agent shell: agent contexts see the
runner-projected `AVA_DB_URL` and agent-profile environment hygiene, so the script
refuses them. See [Data-plane credential split](data-plane-secret-split.md#routine-data-plane-rotation)
for the exact invocation command.

Both scripts are gateway-home scoped, default to read-only, and save 0600 resume
state on failure. The complete upgrade, verification, runner-restart, and
recovery procedure is [Data-plane credential split](data-plane-secret-split.md).

**Provider API key rotation** — mint the new key in each console, then
`ava config set KEY=VALUE` (a merge patch over just that key; it prints
whether a restart is required — see
[`decisions/2026-07-17-config-reducer-semantics.md`](../decisions/2026-07-17-config-reducer-semantics.md)):

| `.env` key | Console |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com -> API keys |
| `OPENAI_API_KEY` | platform.openai.com -> API keys |
| `GEMINI_API_KEY` | aistudio.google.com -> API keys |
| `DEEPSEEK_API_KEY` | platform.deepseek.com -> API keys |
| `GLM_API_KEY` | open.bigmodel.cn -> API keys |
| `MOONSHOT_API_KEY` | platform.moonshot.cn -> API keys |
| `MIMO_API_KEY` | this provider's own developer console |
| `DASHSCOPE_API_KEY` | bailian.console.aliyun.com -> API-KEY |
| `BRAVE_API_KEY` | api-dashboard.search.brave.com |
| `JINA_API_KEY` | jina.ai -> API keys |
| `WANDB_API_KEY` | wandb.ai -> Settings -> API keys |
| `AVA_CF_API_TOKEN` | Cloudflare dashboard -> My Profile -> API Tokens |
| `AVA_TELEGRAM_BOT_TOKEN` | Telegram `@BotFather` -> `/revoke` then `/token` |

Not automated: each is a manual console visit, and several (Telegram) have no
programmatic rotation API at all.

### Manual rotation after a credential leak

No command rotates the database write generation, and none is planned: a leak
is rare, single-operator and supervised, so it is a procedure run by hand.
Every step names an existing tool. Cutting a leaked credential off
means the previous write generation stops being able to log in: its logins lose
`LOGIN`, their sessions are terminated, and their secret files are deleted.

1. **Stop the whole cluster.** On every runner `ava stop -y`; on the gateway
   `ava stop -y --keep-infra` (Postgres and Redis must stay up for the steps
   below). No service may run while the generation changes: the `/ops` server
   reads its accepted tokens once at boot, and the fence terminates whatever
   still holds a session of the old logins.
2. **Human bearer** (only if it leaked; on the gateway checkout, in a gateway
   context): `rotate_cluster_secret.py --execute`. Do it before step 6: the
   telemetry token in every unit bundle derives from this secret.
3. **Redis** (gateway checkout): `rotate_data_plane_secrets.py --execute`
   (`--scope admin` for `requirepass`, `--scope runner` for the ACL runtime
   password; the default is both). Both scripts are described above.
4. **Database write generation** (gateway checkout, home resolved from the
   checkout, `unset AVA_PROCESS_PROFILE`, Postgres up). The fence and the next
   admission of `base.cluster.authority` under one operation id. Keep the
   printed id: a retry after a crash must pass the same one, and the ledger
   holds instead of minting a second pair.

```bash
OP=$(uuidgen); echo "operation $OP"
.venv/bin/python - "$OP" <<'PY'
import sys
from uuid import UUID

from base.cluster import db_identity, get_record, ownership
from base.cluster.authority import (
    OperationAuthority,
    activate,
    mint_generation,
    prune,
    require_ledger,
    verify_generation,
)
from base.cluster.authority.fence import close_revoked, revoke
from base.host.net.url_secret import url_with_port
from base.paths import ava_home
from cli.commands.data_plane import pgbouncer as pooler
from cli.commands.data_plane.bringup import admin_session, db_endpoint, prove_generation_logins

authority = OperationAuthority(operation=UUID(sys.argv[1]), direction="candidate")
home, database = ava_home().resolve(), db_identity()
record = get_record(home)
with admin_session(record, database) as conn:
    revoke(conn, home, authority)  # ledger `revoking`, then the NOLOGIN sweep
    pooler.stop_pgbouncer(force=True)  # nothing may still hold the old pair
    ownership.require_listener(None, record.ports["pgbouncer"], required=False)
    close_revoked(conn, home, authority)  # terminate and count sessions; ledger `closed`
    prune(conn, home, authority)  # drop the closed logins
    mint_generation(conn, home, authority)  # secret, ledger `pending`, the two logins
    generation = require_ledger(home).unrevoked
    direct = url_with_port(db_endpoint(), record.ports["postgres"])
    prove_generation_logins(home, generation, direct)
    print("active generation", activate(home, authority, verify_generation(conn, home)).number)
PY
```

   The fence revokes the active generation (ledger `revoked`), removes `LOGIN`
   and the password from its two logins, stops the pooler, terminates and counts
   every session of them until none is left (ledger `closed`, secret deleted),
   and drops the logins (one with a dependency stays as an inert `NOLOGIN`
   tombstone). The admission mints `ava_g<n+1>_gateway` and `_runner`, proves
   each logs in on the home's own Postgres, and marks the generation `active` in
   `$AVA_HOME/db-authority/ledger.json`.
5. **Start the gateway**: `ava start`. The ordinary start re-checks the group
   grants, sweeps any stale login again, births a pooler serving exactly the new
   pair, and launches every service with the new logins.
6. **Every runner**: on the gateway `ava cluster db-authority issue-unit
   --machine <name> --home <unit $AVA_HOME> --out <bundle>`, carry the bundle and
   its printed transport key separately, then on the runner
   `AVA_DB_CAPABILITY_KEY=<key> ava cluster db-authority install-unit <bundle>` with the unit
   stopped, then `ava start` (the flow in
   [Clusters, units, prod, and dev clone paths](#clusters-units-prod-and-dev-clone-paths)).
   The runner's old login was revoked in step 4, so its previous bundle cannot
   start it. A runner also fetches its Redis URL from the gateway at start, so
   this step is what delivers the rotated Redis password.
7. **Provider keys**: mint each in its console, then `ava config set KEY=VALUE`
   (the table above); the command says whether a restart is needed.
8. **Verify.** `ava status` on every unit; `active.number` in `ledger.json`
   is the new number and every older one reads `closed`; and, as the
   administrator (`psql`, above), `SELECT rolname, rolcanlogin FROM pg_roles
   WHERE rolname LIKE 'ava\_g%'` shows only the two new logins able to log in.

If step 4 fails, read its error: the ledger refuses rather than guess, and the
cause is named (a session that would not close, a prepared transaction, a
foreign pending generation). Fix that, then re-run the same block with the same
operation id.

## Code version gate

Every pooled database session checks the code version of its own process against
`deployment_state.min_code_version` and exits `78` when it is lower
([decision](../decisions/2026-09-30-client-side-code-version-gate.md),
[design](../base/db/docs/code-version-gate.ava.okf.md)). It keeps a unit that missed
an update (a closed laptop) from writing with old code when it wakes. A process's
**code version** is the first-parent commit count of the commit it loaded; the
gateway raises the stored minimum to its own version on every start.

**A routine update needs nothing.** The update script starts the gateway first,
which raises the minimum, and then the runners. Every unit needs a **full** clone:
a shallow one counts fewer commits than exist, so its processes look stale and
exit `78` (`git rev-parse --is-shallow-repository` must print `false`).

**A unit exits `78`.** Its log holds one `critical` line: `code version gate:
process <name> runs code version <v>, below the cluster minimum <m>`. The checkout
is behind the cluster: check out the commit the gateway runs, `uv sync --frozen`,
`ava start`. `ava stop` and every other `ava` command keep working meanwhile, so
a behind host can always be stopped and recovered; only service processes are
gated. A supervisor that revives the stale service gets the same exit again until
the checkout is updated.

**Read the state.**

```bash
git rev-list --count --first-parent HEAD          # this checkout's code version
psql "host=/tmp/ava-pg-<home-slug> port=<pg port> dbname=<db>" \
  -c "SELECT min_code_version FROM deployment_state"   # administrator, as above
```

Running processes name themselves `ava:<process>:v<version>` in the pooler's
client list: `PGPASSWORD=$(jq -r .password "$AVA_HOME/db-authority/pooler-admin.json")
psql "host=127.0.0.1 port=<pooler port> user=ava_pooler_admin dbname=pgbouncer"
-c 'SHOW CLIENTS'` (the pooler port is `AVA_DB_URL`'s port in the gateway `.env`;
read the `application_name` column). The password stays in the environment, never
argv.

**Rolling back to an older commit** must lower the minimum by hand: it never falls
by itself, and an older process would exit `78` at its first dial (the gateway
included).

1. Stop the whole cluster (`ava stop -y` on every unit; the gateway with
   `--keep-infra`).
2. Compute the target's version: `git rev-list --count --first-parent <sha>`.
3. As the administrator, `psql "host=/tmp/ava-pg-<home-slug> port=<pg port>
   dbname=<db>" -c "UPDATE deployment_state SET min_code_version = <target>
   WHERE id = 1"`. `0` switches the gate off until the next gateway start.
4. Check out `<sha>` on every unit, `uv sync --frozen`, start the gateway, then the
   runners. The gateway's start sets the minimum to exactly the target's version.

**Shipping the gate itself is unprotected.** Processes that run code from before
the gate have no check, so the update that first carries it stops every unit by
hand, as updates did before; the gate protects from the following update on.

## Code flow & Events

Kernel-side LLM calls go through `llm.astream()`; a LangChain callback publishes
chat / code / reasoning start + delta to the Redis `ava:events` channel on each
chunk. The full role table (payload fields, publisher, when each fires) is in
`base/events/live/docs/live.ava.okf.md`; interrupt semantics for cancel / terminate are in
`agent/graph/docs/graph.ava.okf.md`.

**Lifecycle command residue:** never hand-clean a stuck lifecycle command (a row
left pending/claimed, or a live `agents_meta.lifecycle_command_id` pointing at a
finished command) with an ad-hoc UPDATE. Settle it through the owning path — boot
recovery, the settle ops, or the repair script — and clear the pointer in the same
transaction: a manual flip to `done` is exactly the torn shape the commit-time
guard rejects (task #3678), and a manual pointer clear without a settle only makes
the failure invisible.



## Observability / Tracing

The observability stack (user decision 2026-08-11, architecture task #1266):
**OTel + Tempo + Loki + Prometheus + Grafana**. The one gateway home carrying
`$AVA_HOME/lgtm-host` and every pure runner run an **OTel Collector sidecar**
(`ava-otel-collector`, supervised by the root and installed by converge
from `deploy/otel-collector/`). Producers export OTLP/HTTP to their local
sidecar (`AVA_TELEMETRY_OTLP_ENDPOINT`, default `http://127.0.0.1:4318`; the
ingress port is the host setting `AVA_TELEMETRY_OTLP_PORT` — source for this
unit's sidecar receiver, authenticated remote receiver and port probes).
When no endpoint is explicitly set, it follows the local port.
Both local endpoint and port stay on the unit; neither is distributed by
bootstrap. An
unmarked gateway skips the collector and the default producer export, keeping
the JSONL event mirror only; an explicitly configured OTLP endpoint opts the
producer into that external collector. Delivery is role-specific: the marked
gateway collector writes traces to the Tempo selected by the host-scope
`AVA_TELEMETRY_TEMPO_ENDPOINT` setting and logs/metrics to gateway-loopback
Loki/Prometheus; a pure runner
collector keeps the same three exporter component IDs and relays each
signal to `AVA_GATEWAY_OTLP_ENDPOINT`, a read-only bootstrap projection of
the gateway's reachable host and OTLP port, with `Authorization: Bearer
<telemetry token>` (from its capability; the gateway derives the same token
from its secret). A runner's local port never
selects the remote port: a Windows receiver on 4318 can relay to a WSL gateway
on 54318 without colliding in mirrored networking. Update the gateway before
runners; a missing or invalid endpoint fails converge rather than guessing a
port. `ava trace ship` uses the same projection. The remote receiver binds
only the exact non-loopback `AVA_MACHINE_HOST`, never `0.0.0.0`/`::`; the
local receiver uses `127.0.0.1:AVA_TELEMETRY_OTLP_PORT` (default 4318) without
auth. Combined single-box deployments keep only the local receiver, including
when their secret is set.
Every application log, metric and trace Resource carries `cluster` = this
home's display label. The gateway collector drops any non-null cluster that
does not match its own, while retaining null-cluster legacy/filelog/infra
resources. It fans out:

- **traces** → Tempo OTLP/HTTP (`AVA_TELEMETRY_TEMPO_ENDPOINT`, default
  `http://127.0.0.1:14318`; prod sets a host-scope override to the remote WSL
  Tempo) + local JSONL mirror
  (`$AVA_HOME/traces/spans.jsonl`, rotated `spans-<ISO>.jsonl`).
- **logs** — every unified event (the emitter's write path) dual-writes to
  OTLP logs (Loki) via `base/telemetry/otlp/telemetry_otlp.py` → sidecar → Loki
  (`AVA_TELEMETRY_LOKI_URL` base, `/otlp` appended). The emitter makes
  `event_name`, `cluster` and, when present, `agent_id` resource dimensions per
  record before the SDK serializes a batch: Loki indexes those resource
  dimensions, so every indexed label is the same value as the event JSON body.
- **metrics** — telemetry events' numeric payloads map to OTLP metrics
  (Prometheus): int -> counter, float -> histogram, named `ava_<event>_<field>`;
  sidecar → Prometheus OTLP receiver (`AVA_TELEMETRY_PROMETHEUS_URL` base,
  `/api/v1/otlp` appended).
- **infrastructure metrics** — the sidecar SCRAPES as well as forwards
  (issue #46): `host_metrics` on every collector-bearing unit (cpu / memory / load / disk /
  filesystem / network) plus, on a gateway-capable unit only, `postgresql`
  and `redis` against **this cluster's own** data plane. Zero extra binaries —
  no node_exporter / postgres_exporter / redis_exporter — because the pinned
  contrib collector already carries the receivers. They ride their own
  `metrics/infra` pipeline (host identity attached there, app metrics
  untouched) and land in Prometheus under `job="ava-infra"` with `host` = the
  OS hostname (physical identity) and `machine_name` = the Ava roster identity
  baked into that unit's config at converge. Dashboards and alerts group by
  `machine_name`. A pure agent-runner's DB/Redis URLs point at the gateway's
  data plane, so its config omits those two receivers entirely rather than
  duplicating the gateway's series. A gateway's Postgres receiver dials its
  own instance over the owner-only socket as the password-less monitoring role
  `ava_monitor` (peer), so the config names no database credential and a
  rollout leaves it scraping; a remote-managed plane omits it. The Redis
  receiver always authenticates with the Redis-admin password.
- **collector delivery metrics** — every sidecar scrapes its per-unit loopback
  self-metrics endpoint every 30s into `metrics/infra`
  (`AVA_OTELCOL_METRICS_PORT`, default 8888). The root probes the same
  endpoint. Grafana rules alert on current queue pressure, new enqueue failures
  over 5m (counter delta, never lifetime absolute value), and a recently-seen
  machine whose collector stopped reporting for 5m. The root logs current
  full queues but does not restart a healthy receiver for remote backpressure.

**Event-label canary.** After an OTLP-emitter rollout, query a post-rollout
window and require every event stream label to equal its JSON event name. This
checks the indexed read path, not merely content filtering:

```bash
.venv/bin/python - <<'PY'
from datetime import UTC, datetime, timedelta
import json
from urllib.parse import urlencode
from urllib.request import urlopen

end = datetime.now(UTC)
params = urlencode(
    {
        "query": '{service_name="unknown_service"}',
        "start": str(int((end - timedelta(minutes=15)).timestamp() * 1_000_000_000)),
        "end": str(int(end.timestamp() * 1_000_000_000)),
        "limit": "2000",
    }
)
with urlopen(f"http://127.0.0.1:3100/loki/api/v1/query_range?{params}") as response:  # noqa: S310 — loopback Loki canary
    result = json.load(response)["data"]["result"]

mismatches = [
    (stream["stream"].get("event_name"), json.loads(line).get("event_name"))
    for stream in result
    for _, line in stream["values"]
    if stream["stream"].get("event_name") != json.loads(line).get("event_name")
]
assert not mismatches, mismatches[:20]
print(f"checked {sum(len(stream['values']) for stream in result)} event rows")
PY
```

An absent `event_name` label or any mismatch fails the canary; run it only over
newly emitted rows, since indexed-era data from before the rollout is immutable.

**One time-series store.** Prometheus holds the host history; nothing else
retains one. `ava status` and the status page carry a single LIVE psutil
reading per machine (`base/host/resource_sample.py`) — the degraded answer for a
deployment whose LGTM backend is down or was never deployed — and link to the
Grafana host dashboard for the trend. The retired `base/resource_monitor.py`
kept a 300-sample ring buffer per process; two samplers meant two answers to
"what was the CPU on machine X" that drift apart, and its history evaporated
on every restart anyway.

`AVA_TELEMETRY_OTLP_ENABLED` does **not** gate these. That flag is
producer-scoped — the event dual-write, trace recording and ship, all things
Ava processes do — and the sidecar lifecycle is independently marker/role
gated. Infra metrics are the collector's own scrapes, so a marked gateway or
runner can report host health while the event stream is reduced to its JSONL
mirror. To silence them, stop the sidecar
(`ava start --disable-service otel-collector`) or the stack (`ava lgtm off`);
with no backend reachable the Prometheus exporter's bounded retry drops them
the same way it already drops app metrics.

**Not covered.** PgBouncer has no OTel contrib receiver (its `SHOW STATS`
admin protocol is not the Postgres wire protocol), so pool saturation is
watched at Postgres — backends against `max_connections`. Per-process
attribution ("which agent is eating the box") is also absent: the
`host_metrics` process scraper is unsupported on macOS, which is what prod
runs, and it filters by process NAME, which cannot separate an Ava agent from
any other `python3.12` on the box.

The whole OTLP surface (exporter + trace recording + ship) is gated by
`AVA_TELEMETRY_OTLP_ENABLED` (default **on**); off leaves the JSONL mirror only
and freezes Loki, Prometheus, and their read surfaces at the last exported
data. There is no Postgres fallback: the `events` archive was dropped (task #1281/#1823). This is
one startup-applied kill switch, so a change requires a process restart. The
home/role producer gate additionally prevents an unmarked gateway from using
the default loopback endpoint; explicitly setting `AVA_TELEMETRY_OTLP_ENDPOINT`
bypasses that gate without creating a local collector.

**LGTM backend lifecycle** — Loki, Prometheus (GOMEMLIMIT 2GiB / 1GiB), and
Grafana run as native processes on Darwin arm64 and Linux amd64, owned by
`ava-root` (below). Explicit host listen ports permit isolated homes; defaults
remain 3100/9090/3003 plus Loki gRPC 9095. See [native lifecycle](../cli/commands/observability/docs/lgtm.ava.okf.md). Tempo is remote, selected by the host-scope
`AVA_TELEMETRY_TEMPO_ENDPOINT` setting. No
service lifecycle depends on a container backend. The backend is required while the gateway serves /ops
and the inspect endpoints (consumers: the gateway Loki/Prometheus read paths,
ops alerting via Grafana's embedded Alertmanager → the gateway webhook, the
events-maintenance Loki rollup, `ava cluster health`). It is a **host
singleton** owned by the lifecycle on exactly one home per host — the
observability station. Provider identity is either the operator-created
`$AVA_HOME/lgtm-host` marker file (in practice prod `~/.ava`;
`touch ~/.ava/lgtm-host` once, or `ava lgtm on`) or the declarative
`observability-station` unit capability (`ava init
--serve-observability-station`, recorded as `AVA_MACHINE_SERVE_OBSERVABILITY_STATION` in the
home's `.env`). On the station home, converge
prepares pins from `deploy/lgtm/native/versions.yml` and rendered configuration.
Loki, Prometheus and Grafana belong to the normal root service roster, on both
macOS and Linux. They have no separate OS jobs. `AVA_LGTM_STORAGE_DIR` selects
retained Loki and Prometheus storage (default `$AVA_HOME/lgtm/native/data`).
Readiness binds successful native protocols to root-owned listener generations.
Loki write/read diagnostics report separately and never launch processes.

The pinned Loki binary must accept `-verify-config` before startup. Configuration
or roster changes require a normal stop before start. `ava lgtm on` and `off`
change the three backend selections through the same start entry and preserve
other service choices and stored data. See `deploy/lgtm/README.md`.
Unmarked homes do not launch backends; explicitly configured remote query URLs
remain independent from local backend ownership.

**Recording is one collector hop from the producer** (sidecar architecture, task #1266). The
previous inline-POST design raised `Exception while exporting Span.` whenever
the POST failed; the agent-side mirror (record/ship split 2026-06-16) fixed
that but left the mirror as the only durable copy. When the home/role gate
allows export, recording is an OTLP export to the configured collector
(normally the local sidecar). Trace and log exporters use 5,000-request
**file-backed sending queues** (file_storage, 1 GiB on-disk byte cap each), so
their accepted backlog survives sidecar restarts; metrics use a 1,000-request
in-memory queue. Every queue rejects the newest batch immediately when full,
and the collector's
enqueue-failure counters make that loss visible. Every send attempt is bounded
to five seconds and every exporter retries for at most 15 minutes before its
counted failure path drops the batch. Cumulative metrics repair their totals on
a later successful sample. `base/telemetry/otlp/telemetry_otlp.py`
also sheds (counted) instead of blocking, so an unreachable sidecar never
touches the main write path. Exporter IDs stay `otlphttp/tempo`,
`otlphttp/loki` and `otlphttp/prometheus`; in particular, renaming Tempo/Loki
would orphan their file_storage backlog during an upgrade.

**Record** — `base/telemetry/tracing.py:initialize_tracing`, gated by `AVA_TRACE_ENABLED`
(default **on**). Instrumentation is OpenLLMetry (`traceloop-sdk`); the sole span
exporter is `OtlpJsonHttpSpanExporter`, which POSTs each export batch as one
standard protobuf `ExportTraceServiceRequest` to the configured collector's
`/v1/traces` (LLM content stripped before it leaves the process). Each batch
gets one 2-second POST; three consecutive failures open a 30-second circuit
that counts and drops later batches without a POST, then admits one recovery
probe. The sidecar's file exporter mirrors each accepted batch as OTLP/JSON to
`$AVA_HOME/traces/spans.jsonl` — the durable, vendor-neutral, grep-able
source of truth (any OTLP backend ingests the same lines; rotation bounds the
directory by size/day/backups, and the agent-start prune enforces
`AVA_TRACE_RETENTION_DAYS` / `AVA_TRACE_MAX_DIR_MB` as the final guard). A
sidecar not answering at agent init reports once and starts one daemon retry
loop. Both the trace precheck and event exporter retry every five minutes; the
event exporter records disabled/recovered attempts as real events in the JSONL
mirror that survives the outage.

**Ship** — `ava trace ship` (`cli/commands/observability/trace.py`). Recovery replay reads
the mirror and bypasses the LOCAL sidecar, because replaying through it would
write the replayed lines back into the mirror (watermark loop). A gateway or
single-box unit POSTs straight to loopback
`{AVA_TELEMETRY_TEMPO_ENDPOINT}/v1/traces` without auth; a pure runner POSTs
to the gateway collector's private port 4318 with its telemetry token. The
remote trace pipeline writes Tempo only and never the gateway mirror, avoiding
a second copy and replay ambiguity. Needed only for gaps the queue could not
hold (backend down longer than the queue, offline machines, past windows).
Gated by `AVA_TELEMETRY_OTLP_ENABLED` (refuses while off — one kill switch
for the whole OTLP surface). The 5-minute ship schedule (gateway schedule
id=5) runs `ava trace ship` on a timer — it ships the local mirror to the
local Tempo viewer (LGTM stack) every 5 minutes, incremental by per-file
watermark.

- **incremental** (no args): a per-file byte-offset watermark
  (`traces/.ship-watermark.json`) advances per POSTed line, so re-running ships
  only new lines and an interrupted ship resumes exactly where it stopped.
- **windowed** (`--since` / `--until`, `YYYY-MM-DD`): ships matching files whole,
  ignoring the watermark — the "shipping was off, import a past range" path. Span
  ingestion is idempotent by span id, so re-shipping is safe.

The OTLP toggle gates both recording and shipping in the collector
architecture: recording itself is export to the configured collector. Existing mirror
files remain on disk while disabled and can ship after re-enabling. (Bench
containers record to their ephemeral-FS mirror, which dies with the container
— a host that wants bench traces in Tempo ships its own mirror after the run.)

**Explicit instruments + per-turn turn_span**: `Traceloop.init` is called
with `instruments={ANTHROPIC, OPENAI, LANGCHAIN, GOOGLE_GENERATIVEAI}` —
LangGraph nests through the LANGCHAIN instrumentor (its callback handler), so
there is no separate LANGGRAPH instrument. Around each per-turn
`graph.ainvoke`, `agent/turn/runloop.py` opens `turn_span(name="ava-agent-N",
session_id=str(agent_id), turn=N)`, a native OTel root span stamped with the
neutral `session.id` (the viewer groups one agent's turns into a session by
it) and `ava.turn`. One trace = one turn: the root span closes and exports
when the turn's work is done. All child spans (LLM calls, tool execs,
retries) share that root's trace_id + parent; without the wrap each LLM call
is an orphan. Positioning: traces are a **drill-down tool for bounded units**
— a finished turn rendered as a waterfall. The primary observation surface
for long-running agents is the unified event river (Loki, via the dual-write
above), not Tempo.

### The operator's SRE loop

Resource oversight is the **cluster-operator agent's judgment over LGTM data**
(user ruling 2026-08-19), never a hardcoded limit in framework code: whether a
saturated box is a runaway or a PyTorch job doing exactly what it was asked
depends on machine specs and co-tenancy, which the kernel cannot know. The
same boundary is why `execute_code` has no compute budget (issue #45).

What the operator watches, and where it reads:

Infrastructure views and alerts group `job="ava-infra"` series by the Ava
roster `machine_name`; `host` remains the OS hostname for physical diagnosis.

| Axis | Read | Alert |
|---|---|---|
| LLM / gateway / turn latency p95-p99 | Grafana `ava-ops-main`, Prometheus `ava_*` histograms | R4 (LLM p95) |
| Error and warning volume | Loki event stream | R1, R6 |
| Delivery and event-pipeline health | Loki | R2, R5 |
| Host CPU / memory / load | Grafana `ava-ops-main` ("Host & data plane" section), `job="ava-infra"` | R8, R9 |
| Per-volume disk | same, `system_filesystem_utilization_ratio` | R10 (and R7, its trace-recording consequence) |
| Data-plane saturation | same, `postgresql_*` / `redis_*` | R11, R12 |

The response is judgment, not a runbook branch: identify the consumer, then
investigate, terminate idle agents to shed load, or tell the user — and
sometimes conclude the machine is busy for a good reason and do nothing. The
thresholds live in `deploy/lgtm/config/grafana/provisioning/alerting/rules.yml`
(the converge-rendered source template — converge copies it verbatim into
`$AVA_HOME/lgtm/native/config/provisioning/`) as deployment-tunable rule
config; a box whose normal state trips a rule wants its threshold edited
there, not a special case in code.

## Logging / diagnostics

Where to look when something went wrong on a host:

| Question | Surface |
|---|---|
| what did daemon X do | `$AVA_HOME/logs/<name>.log` (JSONL, rotated 100MB / 7 days) |
| what did the cluster do, without ssh | `GET /api/cluster/admin/events` over the private network |
| why did a daemon vanish | its log file: every daemon wraps `asyncio.run(main())` and logs the traceback before re-raising |
| what did milvus say | its log file only — it is a C++ binary with no PG sink |
| an agent's exec subprocesses | `$AVA_HOME/logs/agent-{N}.log` (every exec subprocess of the agent appends) |
| raw session stdout (gateway / shells / daemons / schedules) | Loki (the LGTM backend): shell logs → `filelog/sessions`; gateway/daemon/schedule logs → `filelog/services`. Banner-only agent main stdout is excluded. All filelog receivers derive Loki `service_name` from the filename and persist offsets. Loki retains 84 hours; scheduled local cleanup uses the family tiers below. See `deploy/lgtm/README.md`. |

### Local log rotation and retention

`ava logs rotate` copytruncates top-level `$AVA_HOME/logs/*.out.log` files and
top-level `$AVA_HOME/lgtm/native/logs/*.log` files when they reach 64 MiB or
their mtime's UTC date differs from today. A zero-byte file never triggers:
there is nothing to archive, and a stale empty log would otherwise produce a
fresh empty archive every day. Grafana logs are excluded because
Grafana rotates itself, as are already dated `*.log.YYYY-MM-DD` archives. The
archive suffix is today's UTC date; an existing archive makes that file a
same-day idempotent no-op. Copytruncate preserves the live path and inode, so a
writer keeps its open file descriptor.

`ava logs retention` removes expired allowlisted files from those two
top-level roots plus the nested computer-use snapshot dir
`$AVA_HOME/logs/computer/snapshots/`. The allowlist covers agent-main
`ava-agent-<id>.out.log`, named PTY
`ava-agent-<id>-shell-<n>-<name>.{out,host}.log`, every service
`ava-*.out.log`, Loguru rotations named
`<service>.YYYY-MM-DD_HH-MM-SS_<pid>.log`, the dated service/native archives
created by `ava logs rotate`, and computer-use snapshots `agent-<id>-<stamp>.png`
(7 days). Rotation stays top-level-only; retention reads the fixed roots above
without general recursion, and neither command follows symlinks; retention also
skips every file held open by a visible process.

Preview the exact paths, UTC mtimes, sizes, and total bytes before deleting:

```bash
ava logs rotate --dry-run
ava logs rotate
ava logs retention --dry-run
ava logs retention
AVA_LOG_RETENTION_DAYS=21 ava logs retention --dry-run
ava logs retention --older-than 21
ava logs retention --family-days agent=15,shell=7,gateway=30,ops=30,watchdog=30,snapshot=7,other=3 --dry-run
```

The age is a positive integer number of days. `--older-than` and
`--family-days` are mutually exclusive. Without either flag, the legacy global
threshold remains: `AVA_LOG_RETENTION_DAYS`, otherwise 14 days. `--older-than`
is the explicit global override. `--family-days` activates the C baseline:
agent-main, `ava-agent-*` service stdout, and their archives 15 days; named PTY
shell transcript/host files and computer-use snapshots 7 days; `gateway*`,
`ops*`, and `*-watchdog` / `*_watchdog` service files and rotations 30 days;
all other service and native
archives 3 days. The rotation shape also admits underscores, so
`delivery_watchdog` is in the watchdog family. Supply only the family values
that differ; omitted values retain that baseline. In a mapping, `default=N`
aliases `other=N` for the catch-all service family.

Dry-run candidates include their family and selected days, followed by one
`retention_family` line per policy family (including zero-candidate families)
with its candidate count, days, and bytes. A file exactly at its cutoff is
retained (`mtime < cutoff` is deleted). Delete failures are reported per path on
stderr, remaining candidates are attempted, and the command exits nonzero if any
inspection or deletion failed.

Converge registers one low-traffic daily job per machine. macOS launchd and
Linux cron run rotation followed by retention at 04:40 local time; the second
command runs only when rotation succeeds. Re-converge replaces the job definitions idempotently, and cluster destroy
removes them.

Raw session output is queried in Loki, not tailed from a file — Grafana Explore
(Loki datasource), `logcli`, or the HTTP API:

```bash
logcli --addr http://127.0.0.1:3100 query '{service_name="ava-gateway"}' --since=1h --limit=100
logcli --addr http://127.0.0.1:3100 query '{service_name=~"ava-agent-.+-shell-.+"}' --tail
curl -G -s http://127.0.0.1:3100/loki/api/v1/query \
  --data-urlencode 'query={service_name="ava-gateway"} |= "error"' \
  --data-urlencode 'limit=50'
```

Raw filelog streams and the OTLP event stream both use `service_name`; filelog
values are session names such as `ava-agent-1818-shell-1` or `ava-gateway`.
Agent loguru JSONL (`agent-{N}.log`) is not scraped — it already reaches Loki
structured via OTLP.

The emitter wiring behind that stream, the unified `events` schema (and its
legacy `agent_events` mirror), and the monthly partitioning are in `base/log/docs/log.ava.okf.md`.

## Git hooks: pre-commit / pre-push

On development clones, prefer installing from the **main clone**, using its
stable `.venv`:

```bash
.venv/bin/pre-commit install --hook-type pre-commit --hook-type pre-push
```

When the main clone uses a protected runtime venv without pre-commit, use a
stable user-level runner instead; run the install command from the main clone:

```bash
env -u VIRTUAL_ENV uv tool install pre-commit
~/.local/bin/pre-commit install --hook-type pre-commit --hook-type pre-push
```

The configuration also makes plain `pre-commit install` install both stages.
Never install from an ephemeral worktree: all worktrees of a clone share
`git rev-parse --git-common-dir`'s `hooks/` directory, and pre-commit writes the
installing interpreter's absolute path into `INSTALL_PYTHON`. Deleting that
worktree leaves a dead pointer. `scripts/setup-worktree.sh` checks the shared
installation (`check_git_hooks.py --strict`: a problem stops the bootstrap) without rewriting hooks or `core.hooksPath`. Converge re-asserts
the same health on every source `ava start`
(`ensure_local_git_hooks` runs `check_git_hooks.py --scan-machine` over the
conventional local checkouts); drift warns, it never blocks. Before deleting a
worktree, inspect both shared hooks' `INSTALL_PYTHON` values and reinstall from
the main clone if either points into the worktree.

The fast `check-git-hooks-install` commit hook checks both shared pre-commit and
pre-push hooks and reports every problem: a hook is missing, unmanaged,
non-executable, dispatches the wrong stage, is redirected by `core.hooksPath`, or
points at a missing/non-executable interpreter or a disposable `worktrees/` or
`.worktrees/` path. Executable Python paths in stable user-level uv tool
installations pass. It prints the repair commands and passes: v1 is warn-only so
hook rollout does not block commits; CI independently enforces the checks.
Review any `core.hooksPath` override before removing it and installing.

A commit hook's cost follows the change, not the repository. The per-file
Python and content lints take the changed files (`--only FILE...`; the contract,
and what widens the run to a full scan, is the
[changed-files mode](../scripts/lint/docs/changed-files-mode.ava.okf.md)). The
code-generation freshness hooks, whose check needs the whole project, run only
when one of the inputs in their `files:` filter changed. ESLint lints the changed
frontend files (`scripts/precommit-eslint.sh`) and the whole project when the
lint setup itself changed. A hook that scans the whole repository on every
commit regardless of the diff is a design error, not a price of the check; CI's
`pre-commit run --all-files` is the full scan of every hook and the merge gate.
Frontend typecheck (including Next.js route typegen), the whole-project ESLint
run (`frontend-eslint-full`: a type-aware rule can react to a type that changed
in another file, which the per-file run cannot see) and the full Vitest suite run
at **pre-push**, scoped by their own `files:` filter to frontend changes. Strict
pyright also runs at pre-push, but scoped to the branch's own changed `.py`
files (`scripts/prepush-pyright-files.sh`, `merge-base(origin/main,
HEAD)..HEAD`, ACMR + still on disk) — never the whole repository locally
(user ruling 2026-09-22: local runs check only the files touched; a
full-repo strict pyright stays CI-only, `backend-static`'s `uv run pyright`).
Other local hooks default to pre-commit; upstream hooks may also declare
pre-push hygiene checks. Run either stage explicitly with the worktree's own
environment:

```bash
.venv/bin/pre-commit run --all-files
.venv/bin/pre-commit run --all-files --hook-stage pre-push
```

For targeted local verification, skip `frontend-vitest` by name at pre-push
and run only the relevant vitest files, as described in the
[local-test skill](../.agents/skills/run-local-tests/SKILL.md).
The [Vitest placement decision](../decisions/2026-09-24-vitest-prepush-selection.md)
records the measurements, push-cost projection and affected-test-selection limits.

Three more pre-push-only hooks close gaps: two that `git rebase` / `cherry-pick` /
`merge` leave open (they never invoke the pre-commit hook for the commits they
create), and one that a per-file verdict cannot see:

- `lint-prepush-branch-diff` re-runs the whole pre-commit stage, filters and
  all, over `git merge-base origin/main HEAD`..`HEAD` instead of whatever two
  endpoints pre-commit's own push-time selection would use — so a
  conflict-resolution or cherry-picked commit gets checked before it can
  reach `git push` unchecked. The commit-stage hooks judge only the files they
  are handed, so this costs about what one commit over the same files costs;
  that is why it takes no load threshold and no lock (a load-dependent skip
  would leave rebased commits unchecked at random). It skips loudly only when
  it cannot know the range (`origin/main` is not locally resolvable — fetch
  first) or has no `.venv/bin/pre-commit`.
- `lint-prepush-artifact-freshness` (`scripts/provision/prepush_freshness.py`)
  re-checks, over the whole repository, the generated-artifact hooks (types and
  constants codegen, the events registry, the config-lite table, the Pyright
  tests environments, OKF lint, doc references) whose inputs the branch
  DELETED. A `files:`-filtered hook never sees a purely deleted path on any
  range — pre-commit's own diff selection passes it only
  Added/Copied/Modified/Renamed paths — so a change that only deletes the last
  file behind an event or config field can otherwise reach `git push` with a
  stale artifact and nothing local catching it. A whole-repository hook whose
  inputs the branch also added or changed already ran in the branch-diff run,
  which judges the deletion with it, so it is not repeated; a per-file hook
  (`lint-ava-okf`) sees only the files it is handed and is re-checked whenever
  a deleted path matches. A rename counts as deleting the old path; with no
  known range every hook runs. `types-codegen-fresh` alone skips when
  `ui/web/node_modules` is missing, same as the frontend pre-push hooks.
- `lint-patch-targets-full` runs the patch-target lint over every test file
  when the branch touches any `.py` path, deleted ones included
  (`scripts/prepush-if-changed.sh`; a push with no Python cannot move a test's
  home). A test's home follows what its subject's package imports, so a
  production import change can move the home of a test the commit-time
  `lint-patch-targets` never receives; CI's structure job scans everything too.
- `lint-tests-location-full` runs the tests-location check over every tracked
  top-level test at every push, unwrapped (paths only, under 0.1 s, so there is nothing
  to skip). A deleted or renamed test is never passed to the commit-time
  `lint-tests-location`, so its stale registry entry is found here; CI's structure job
  checks everything too.

`scripts/prepush-guard.sh` holds a separate lock for each of `pyright`,
`tsc`, `eslint` (the whole-project run), and `vitest` across all worktrees on the host.
The lock is `fcntl.flock(2)` on the fd bash opens via `exec 9<lock_file`, run
from a fresh `python3 -c` subprocess per attempt — not the `flock(1)` binary,
which stock macOS does not ship — bound to the *open file description* fd 9
refers to, so it stays held after that python3 process exits, until the
guarded command (exec'd in the same shell process, inheriting fd 9) itself
finishes. A wait-with-timeout is `LOCK_NB` polled in a loop, since `fcntl` has
no built-in timed blocking wait. Locks live in `/tmp/ava-prepush-locks`,
independent of clone, user, and `TMPDIR`; never delete live lock files.
`AVA_PREPUSH_LOCK_DIR` may override this for tests or a host policy, but every
checkout on that host must use the same local directory. Missing
tools/dependencies, unavailable load probes, excessive load, or a lock
timeout print **WARNING: PRE-PUSH SKIPPED** with the tool and reason, then
exit 0. Hook verbosity makes these successful skips visible. Install frontend
dependencies with `(cd ui/web && npm ci)`; Python dependencies use
`env -u VIRTUAL_ENV uv sync` after the worktree venv preflight above.

`AVA_PREPUSH_LOCK_WAIT_SECONDS` bounds how long a contended push waits for a
tool's lock before it skips; waiting prints the tool name.
`AVA_PREPUSH_MAX_LOAD_PER_CORE` is the one-minute load average per logical CPU
above which a heavy tool skips rather than add work to a sustained CPU queue;
load is checked before and after acquiring the lock. Both exist for the heavy
tools only: a hook light enough to run every time takes neither (the nested
branch-diff run), because a skip that depends on host load makes a check present
on some pushes and absent on others. The tool's actual failure status propagates
unchanged; a local skip is never
evidence that the check ran. CI bypasses the wrapper: `backend-static` runs
`uv run pyright` (full repository — CI is where the complete strict pass
lives); `frontend` runs `npx next typegen`, `npx tsc --noEmit`, `npm run lint`
and `npx vitest run --coverage`. These independent runners retain
enforcement; the structural CI job already skips the four duplicate hooks.

## CI (Continuous Integration)

CI runs on **GitHub-hosted `ubuntu-24.04` runners** via the workflows in
[`.github/workflows/`](../.github/workflows/): `ci.yml` (backend pytest +
pyright, frontend eslint + tsc + vitest, e2e Playwright happy path) and the
release and repository-automation workflows. A fork gets CI for free — GitHub
Actions provisions the runners, no self-hosted infrastructure required. The test suite, migration
smoke, and e2e self-provision **throwaway native** pg/redis clusters per xdist
worker (`tests/_containers.py`: `initdb` + `redis-server` on ephemeral
127.0.0.1 ports, data dir on a tmpfs, torn down after), so a runner only needs
the toolchain (Python, Node, uv, Postgres + Redis server binaries; e2e also
needs Playwright chromium) — no Docker, no shared engine.

The required `backend structure (pre-commit lint + codegen freshness)` check
keeps one job with two segments when the frontend/backend classifier selects it:

- **Structure lint (A)** always runs `pre-commit run --all-files`, skipping the
  hooks owned by other CI jobs, the local installation warning, and the four
  codegen freshness hooks. The pyright, frontend-tsc, frontend-eslint and
  frontend-vitest SKIP entries name checks other jobs own (`frontend-eslint` is the
  changed-files commit hook; the other three run only at pre-push); CI runs their
  underlying whole-project checks directly in `backend-static` and `frontend`.
- **Codegen freshness (B)** installs Node/frontend dependencies and explicitly
  runs `types-codegen-fresh`, `constants-codegen-fresh`, `events-registry-fresh`
  and `config-lite-table-fresh`. On a PR, the selector compares the fetched,
  event-pinned base revision with HEAD and matches the changed paths against
  these hooks' `files:` regexes in `.pre-commit-config.yaml`. Rename detection
  is disabled so moving an input out of the union still checks its deletion.
  Any selector exception (including config/YAML/regex failures) warns and runs
  B. A failed selector outcome or missing output also runs B; only a successful
  no-match skips it and prints
  `STEP SKIPPED: codegen freshness` with the reason and safety nets in the log.

CI closure tests follow route annotations and imports to the OpenAPI components'
defining modules, and recursively follow the event contract's source imports.
A model or event definition moved into an uncovered file fails these tests;
update the hook's `files:` regex alongside the move.

Main pushes always select B within this job. The Trunk merge queue re-evaluates
the selector on the combined tree before landing; required check names and
failure reporting are unchanged. CI remains the merge gate, including when
local pre-push checks visibly skip for load or missing tooling.

## Root-owned application lifecycle

`ava start` initializes or resumes the home and reconciles its selected services
under one `ava-root`. Omitted service flags preserve the prior selection.
`--only-service NAME` records an allowlist, while `--all-services` explicitly
returns to the whole applicable roster. Repeating an unchanged start retains
healthy process generations. A successful start requires the complete selected
roster to pass identity-bound readiness, including frontend; there is no waiver.

The platform boundary is explicit:

- macOS: `launchd -> signed permissions helper -> ava-root -> application services`.
- Linux: `systemd/direct launch -> ava-root -> application services`; no helper.

The supervisor name does not imply UID 0. The macOS helper carries the stable
signing identity used for permissions. Normal start observes an unchanged loaded
helper; it never reloads a live ancestor or silently falls back to direct launch.
A new helper artifact can be built at `AVA_PERMISSIONS_HELPER_ARTIFACT_DIR`.
A loaded helper is never replaced. When the helper sources change, `ava stop`
retires the exact-home job, and the next `ava start` rebuilds the stale
`$AVA_HOME/helper` artifact in place under the same designated requirement
(TCC grants carry over); it still refuses while the job is loaded, its plist
remains, or a live process runs the old executable. Rebuilding needs the login
keychain, so a host that cannot sign keeps the old artifact and fails the start.

Root records native process birth and outstanding custody before spawning.
Uncertain leftover ownership blocks a second generation. Service readiness checks
protocol behavior and the captured native owner; an unrelated listener or missing
inspection cannot certify the service. Application stop and persistent terminal
stop are distinct operations, and native data-plane shutdown has its own verified
boundary. A full destroy marks the home detached only after cleanup succeeds.

Root framework tests under `tests/services/test_ava_root_*` exercise supervision,
custody, IPC and readiness; an isolated native cluster is still required to verify
platform ancestry and actual application execution. There is no local branch-preview
controller: validate a branch with CI, the throwaway test clusters, and the Linux
verification container (`python3 scripts/verify/container.py --ref <ref>`: a fresh
container, the commit cloned to `~/.ava/source`, `ava init` and the first `ava start`, a scripted agent;
[verification boundaries](../future/infra/verification-boundaries.md)); what only macOS can show
(the signed helper chain, the desktop grants) runs in a throwaway Tart VM cloned from the golden
image (`python3 scripts/verify/tart_run.py --ref <ref>`, same flow, plus the helper chain check).
