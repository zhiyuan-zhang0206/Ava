# Runtime model

## Clusters, units, prod, and dev clone paths

A **cluster** = one logical deployment. Every cluster — including `main` — owns its
OWN Postgres + Redis instance (under its `$AVA_HOME`, on a per-cluster pg/redis
port), so co-located clusters share no data plane at all: isolation is
home-directory isolation, not a database name / redis logical-DB index / channel
prefix kept correct inside one shared instance. A cluster also owns one outward
gateway and a contiguous host-port block. (The rationale — and the remaining slice 3,
bundling the pg/redis binaries — is in
`future/infra/embedded-per-cluster-data-plane.md`.) A data plane is **swappable**:
URLs naming a foreign host (another machine or a SaaS provider) make the cluster
treat it as remote-managed — `ava start` / `ava stop` / `ava status` / the watchdog
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
provisioning — is the ONLY direct-Postgres consumer, deriving the direct URL
from the registry record (`shared.db.direct_db_url`); everything else dials
`AVA_DB_URL` as-is. Flipping `AVA_PGBOUNCER_ENABLED=false` is the kill-switch:
converge rewrites the URL to the direct port on the next `ava start` and the
pooler never starts.

Fresh install creates LangGraph's checkpoint schema with
`PostgresSaver.setup()` as the cluster owner. Start never calls setup: after
applying Ava's tracked SQL files, every capability reads the complete
`checkpoint_migrations` set and requires the explicitly approved version.
Checkpoint readers and agent boot therefore need CRUD but no schema CREATE.

`AVA_CHECKPOINT_INTERVAL` defaults to `4`, which writes every fourth super-step
(about 75% fewer checkpoint writes before terminal flushes). A crash can replay
up to three super-steps (re-spending LLM tokens and possibly replaying tool side
effects); claimed/pending reconciliation re-delivers inbounds, and checkpoint
parent chains span four steps. Set a per-agent `{"checkpoint_interval": 1}`
config overlay plus an agent restart, or set `AVA_CHECKPOINT_INTERVAL=1` in the
cluster `.env`, to restore every-super-step persistence. The full recovery
verification and rollback protocol lives in
[`docs/conventions/checkpoint-interval-canary.md`](../docs/conventions/checkpoint-interval-canary.md).

An upstream dependency bump that adds checkpoint migration version N must ship
that DDL as a paired Ava timestamp migration and advance
`CHECKPOINT_SCHEMA_AVA_MIGRATIONS` (the upstream baseline stays frozen at 9).
The up SQL must be idempotent when fresh
install setup already created both its schema effects and its
`checkpoint_migrations` row, while still letting Ava record its own migration
name; the down SQL reverses the schema effect and deletes the upstream version
row. Real-Postgres tests must cover both existing-N-1 update/down and fresh-N
birth -> first-start registration/down. Until all of that ships together, the
dependency-drift gate fails before any database mutation, preserving update
recovery and automatic rollback.

**Identity is the home path** — there is no cluster name; the display label is
the home's basename. A cluster's database and the Postgres role that owns it
share one identifier, carried by its `.env` connection URLs **as data**
(`shared.cluster.identity_from_url`): a fresh birth writes the fixed `ava`;
prod stays on its historical `ava_main` until an ops rename edits the URLs.
The role is `NOSUPERUSER` owning only its own database, provisioned by that
instance's own `initdb` superuser over its private loopback-`trust` unix
socket, never by the runtime role.

A **unit** is one install of Ava under its own `$AVA_HOME`, and `AVA_HOME`
locates the unit's `.env`, logs, memory pool, milvus data, pidfiles, etc., all of
which derive from it. The home is resolved **checkout-anchored** by
`shared/dotenv_boot.py:resolve_ava_home` — see "How a unit finds its home"
below — so a bare invocation (ad-hoc script, subagent) inside a dev worktree never
silently falls back to the prod home:

A machine carries a **capability set** — `gateway`, `agent-runner`, or both:

- **gateway** capability: owns the HTTP gateway + the data plane (Postgres /
  Redis / Milvus) + the gateway daemons for its cluster.
- **agent-runner** capability: hosts agents and the ops server, using agent-host
  in hosted mode or the process supervisor/restarter in process mode; its
  DB/Redis/Milvus URLs point at a gateway node when the host carries no
  `gateway` capability of its own.

A **single-box** deployment is one machine, one `$AVA_HOME`, role
`gateway,agent-runner` — it owns the data plane *and* runs agents, and `ava
start` brings up the union of both capabilities' services. A gateway-only or
runner-only node is the explicit split: `install.sh --role gateway` /
`--role agent-runner` scaffolds a single-capability unit. When a gateway-only
unit and a runner-only unit are co-located on one machine they live in separate
homes (the gateway unit at `~/.ava_gateway`) so they do not share state; the
runner unit's home is `~/.ava`.

**Cluster identity** is born at install time (`scripts/install.sh` →
`python -m cli.install_cluster`): the install allocates the home-keyed registry
record + port block, brings up the cluster's own pg/redis, provisions the
database, writes the cluster `.env` (secret follows the role: single-machine =
NO-AUTH empty secret by default, gateway-only = minted,
`AVA_INSTALL_CLUSTER_SECRET` states one explicitly; serve flags from `--role`), and — for `--worktree` —
writes the checkout's `.ava_home` pointer.
The home is resolved **checkout-anchored** (`resolve_ava_home`), *not* from the
current directory and *not* from any flag — no name exists, so no env/cwd/flag
can point an `ava` at the wrong cluster: the prod `ava` on PATH always acts on
`~/.ava` no matter where it runs. `ava start` is a **pure bring-up** and fails
fast on an uninstalled home, pointing at `install.sh` (gateway-capable),
`install.sh --worktree` (unanchored dev worktree), or `ava enroll` (pure
agent-runner). It fails the same way when the home IS registered but its `.env`
names ports the record did not allocate (gateway / Postgres / Redis) — the state
a home is left in when a destroy frees its block and a later birth takes it —
since starting would bind ports the registry has promised to another cluster.
The fix is re-running the install for that home, which re-derives `.env` from
the record. The record is stored in a host-level JSON registry at
`~/.ava/clusters.json` (`AVA_CLUSTER_REGISTRY`), keyed by home path (converge
idempotently rewrites a pre-cutover name-keyed file). An agent-runner's cluster
identity IS the gateway URL + secret it enrolled with (no name travels in the
`/api/bootstrap` payload); its connection facts are NOT stored locally — since
the 2026-08-01 config refactor every process on an enrolled runner fetches
`GET /api/bootstrap` at startup (Settings build, `shared.bootstrap`), with
fetched values authoritative over env/.env. A bare checkout with no role flags
(CI, lint scripts) and a not-yet-enrolled runner construct Settings from local
env/.env with no fetch — `ava start`'s preflight gate (AVA_GATEWAY_URL check)
is what refuses the unenrolled runner. **Startup order: bring the gateway up first, then
the runners** — a runner's `ava start` (and every daemon boot) fails fast until
the gateway answers /api/bootstrap, and recovers on its own once it does (the
boot policy retries `ava start`; the OS watchdog probe revives a daemon that
died at boot).

**Session names** are `ava-<service>` (composed via
`shared/cluster/derive.py:session_name()`; neither machine nor cluster is encoded —
session hosting is host-local AND per-home: native session records under `$AVA_HOME/run/sessions/`
(services / orchestration / agent processes), pty session records + per-session
sockets under `$AVA_HOME/run/pty/` (agent shells / watchers), so the home
already scopes every session). Every cluster produces e.g.
`ava-gateway`; two clusters are two homes, never one namespace.

**CI is a separate hosting surface.** The workflows provision isolated native
test infrastructure; see [CI](#ci-continuous-integration). Neither tests nor
build tooling may target a production cluster home. Container assets elsewhere
in the repository do not establish a cluster runtime dependency.

**Migration note (session rename)**: this naming dropped the machine segment and
added the `ava-` prefix (old convention was `<cluster>-<machine>-<service>`). After
the upgrade lands, the old-named sessions become orphans. The converge step
`_reap_legacy_sessions` (`cli/commands/_converge.py`, run on every `ava start` /
`ava cluster update`) does a one-shot kill of any session matching the old
`<cluster>-<machine>-*` prefix on this host. This boundary has **NOT been tested**
in a live prod upgrade; manually killing the old-named session (its record lives
under `$AVA_HOME/run/sessions/`) is the fallback if the reaper misses one.

**Port blocks** per cluster come from a contiguous `port_base + offset` block,
scanned and allocated by `ava start`. The default (`main`) cluster keeps its
current ports (gateway 8000, frontend 3000, daemon healthz 8101-8106, milvus 19530)
as the legacy seed. Watchdog probe URLs + daemon/milvus/frontend ports derive from settings.

prod runtime and dev workspace are split at the filesystem level:

| Path | Role | Notes |
|---|---|---|
| `$AVA_HOME/source/` (default `~/.ava/source/`) | **prod** — cwd of the long-running service sessions | git working tree; upgrades go through the CLI `ava cluster update` (`ava.self.update()` was removed 2026-08) |
| `~/Ava/` | **dev clone** — root of worktree-driven development; dev worktrees live under `.worktrees/<task>/` (manual / agent-created) or `.claude/worktrees/<task>/` (Claude Code's native worktree tool) | freely checkout any branch, decoupled from prod |

### Worktree uv iron rule (Tasks #1572, #5638)

An editable install is a pointer stored in the **active virtualenv**, not a fact
derived from the shell's current directory. A worktree's `.venv` must be a
real directory inside that worktree, never a symlink. Before **every** worktree
sync, run the dependency-free preflight; it refuses an external environment,
a symlinked `.venv`, or editable records naming another checkout. Then discard
an inherited `VIRTUAL_ENV` for the `uv` command:

```bash
python scripts/guard_editable_venv.py .
env -u VIRTUAL_ENV uv sync
env -u VIRTUAL_ENV uv pip install -e .
```

On PowerShell, apply the same rule with `Remove-Item Env:VIRTUAL_ENV
-ErrorAction SilentlyContinue` before `uv`. Never rely on `cd` alone to select
the worktree's `.venv`. `scripts/install.sh --worktree` and
`scripts/setup-worktree.sh` invoke the same preflight before their own sync.

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
worktree: run `env -u VIRTUAL_ENV uv sync` from the affected stable checkout
and recheck. `ava converge` / `ava start` independently assert and auto-repair
both prod records, then make their site-packages directories read-only outside
the narrow update/repair write window. Every `execute_code` spawn also checks
the current interpreter's records: the first poisoned call repairs the install
and returns a retryable structured error, preventing a flood of failed child
imports. The dev-clone pointer remains part of this mandatory deletion check.
This is the operating half of the editable-install guard specification; the
incident and escape analysis are in
[`postmortems/0006`](../postmortems/0006-an-editable-install-is-a-cross-checkout-pointer.md).

A typical small deployment runs the **gateway as a single-box unit** on an
always-on host (`gateway,agent-runner`, one home `~/.ava`, code `~/.ava/source`,
`main`'s own PG/Redis instance running **natively** via `pg_ctl` + `redis-server`
under `~/.ava` on ports 5433/6380, no docker). Any other
agent-runners stay single-home `~/.ava` and reach that gateway + DB/Redis over
the private network. A larger deployment splits the gateway onto its own
gateway-only host (the explicit `--role gateway` install).

**How a unit finds its home** (`shared/dotenv_boot.py:resolve_ava_home`, run
before `Settings` is constructed). Which checkout the code lives in is the
prod/dev discriminator, so resolution is anchored to `__file__`, not cwd —
identical no matter where a bare script is launched. Precedence:

1. `AVA_HOME` env var — explicit; what a gateway-launched subprocess and the prod
   service sessions set.
2. checkout == `~/.ava/source` (the prod source) → `~/.ava`.
3. `<checkout>/.ava_home` pointer file → the home it names. `ava start`
   writes this into a dev cluster's worktree (gitignored), so every later bare
   invocation from that worktree resolves to the cluster's own home.
4. otherwise → `~/.ava`, but flagged **unanchored**: a dev checkout that was never
   `ava start`'d and carries no explicit `AVA_HOME`. `load_ava_env` plants a
   sentinel `AVA_DB_URL` (`UNANCHORED_DB_SENTINEL`, an unreachable loopback URL) so
   a DB connection fails loud — `shared/db.connect`/`pool` raise `UnanchoredHomeError`
   directing you to `ava start` — instead of silently writing to the prod
   database the host `.env` points at. `pytest` is unaffected: `tests/conftest.py`
   plants its own DB sentinel before import and the container fixtures override it.

Rule 1 only outranks rules 2-3 while they agree. When `AVA_HOME` names one home
and the checkout claims another, resolution **refuses** with
`AvaHomeContradictionError` naming both sides — a process holding one cluster's
`.env` (database, secret, ports) and another's code (`migrations/`, skills,
plugins) has no correct home to pick. That combination is how a fleet agent
migrated prod on 2026-07-31: every agent shell inherits `AVA_HOME=~/.ava`, so a
bare `ava start` in a freshly installed worktree resolved prod while running the
worktree's migrations ([decision](../decisions/2026-07-31-ava-home-vs-checkout-contradiction.md)).
Set `AVA_HOME_OVERRIDE=1` to authorize it where the mixing is the point:
`install.sh --worktree` (it writes the pointer), `ava cluster down/destroy`
(this checkout's `ava stop` against another home), and the test suite's scratch
home. It is read from the real environment only — the check runs before any
`.env` is loaded, so no cluster can grant itself the exemption on disk.

`.env` lives at `$AVA_HOME/.env`; each co-located unit carries its own. On a
single-home machine prod (`~/.ava/source` → `~/.ava`) and dev worktrees resolve to
*different* homes via the rules above, so they no longer share one `.env` by
accident — a dev worktree only reaches a real database after its `ava start`.

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
(`cluster-pinned` / `cluster-default` / `host` / `agent`) in `shared/config/`,
and `BOOTSTRAP_FIELDS` is derived from it.

Postgres and Redis run as native processes (no Docker — the binaries come from brew's
`redis@8.2` keg on macOS / apt on Linux, but Ava drives them directly via `pg_ctl` + `redis-server`,
not `brew services`/launchd/systemd). Every cluster — including `main` — brings up its
OWN pair under `$AVA_HOME` on its per-cluster ports (`cli/commands/_cluster_instance.py`):
`initdb` into `$AVA_HOME/pg` (template-cached through a host-level dir beside the
registry, so a new cluster / a test spins up by directory copy rather than a fresh
multi-second init), plus `redis-server` with its data dir under `$AVA_HOME/redis`.
`ava start` ensures this cluster's instance is up and `ava stop` tears it down — there
is no standalone infra verb, and no shared host instance to survive across
checkouts/worktrees.

The data-plane posture is uniform — the default is multi-machine, a single box is just
the case where the reachable address is loopback (no single-vs-multi branch). When the
control-plane bearer is set, Postgres authenticates its owner role with the gateway-only
`AVA_DB_ADMIN_PASSWORD`; Redis authenticates its `default` administrative user and
`requirepass` with the gateway-only `AVA_REDIS_ADMIN_PASSWORD`; and the Redis ACL runtime
identity uses `AVA_REDIS_PASSWORD` embedded in `AVA_REDIS_URL`. The runner database role
has its separate `AVA_RUNNER_DB_PASSWORD`, embedded only in its projected URL. The bearer
never authenticates the data plane. An EMPTY bearer — the single-box default — keeps all
credentials empty and serves everything unauthenticated on loopback. Postgres loopback
stays `trust`, so the owner password is consulted on TCP connections. Settings re-applies
only the owner password to a main-identity DB URL; it leaves the Redis runtime URL
verbatim. On the same load, a data-plane URL whose host is this machine's own reachable
address (`AVA_MACHINE_HOST`) dials `127.0.0.1` instead (`shared/config/data_plane.py`):
self-dial never leaves the box. The `.env` value, bootstrap payload, and registered
address stay untouched, so remote runners keep dialing the gateway's real address.

**Least-privilege runner role** (`ava_runner`, Task #1236): runner processes do
not dial the main data-plane identity. Every bootstrap projection returns
`AVA_DB_URL` projected onto the fixed `ava_runner` role (LOGIN
NOSUPERUSER NOCREATEDB NOCREATEROLE), whose grants cover exactly the audited
runner surface: SELECT on every table (plus sequence USAGE), SELECT/UPDATE on
`agents_meta` (status/liveness), SELECT/UPDATE/INSERT on `inbound_messages`
(claim AND the agent-side self-lifecycle inbounds — `ava.self.terminate` /
`restart` / `compact` insert their own rows), SELECT/UPDATE on `agents`
(`ava.self.set_label` writes the agent's own row), INSERT/UPDATE/SELECT on
`machine_units` + INSERT/UPDATE on `machines` (register_self / mark_stopping
— `ava start` / `ava stop`), INSERT/UPDATE on `host_deploy_state`
(set_posture), INSERT/UPDATE/DELETE on `api_idempotency` (the runner's ops
server dedupes /ops calls), INSERT/UPDATE on `agent_tasks` (`ava.tasks`),
INSERT/UPDATE/DELETE on `agent_watchers` (`ava.watcher`; DELETE is the
runner-side row removal — clean-exit / kill / reconcile drops), UPDATE on
`agent_pages` (page close at
exit), INSERT on `agent_shell_ttls` (TTL deadline rows; the gateway
reaper reads and deletes them), and full CRUD on the LangGraph checkpoint
tables. `agents` INSERT,
`agents_meta` INSERT, notices writes, the cluster deploy-state tables and any
DDL fail under it by construction — the 2026-08-12 pollution class (full write
credential on the runner) is structurally impossible. That table-wide SELECT is
granted per object rather than as a standing policy, so a migration that CREATES
a table would otherwise leave the new table unreadable to every runner for the
life of the cluster: `ava start` on a gateway host re-affirms the grants whenever
it actually applied a migration, and an `ALTER DEFAULT PRIVILEGES` declared FOR
the cluster's main identity (the role migrations run as) covers everything
created after that first re-affirm. Its password (`AVA_RUNNER_DB_PASSWORD`) is minted
at install (or by `ava cluster ensure-db-role` on pre-cutover clusters),
kept in the gateway's `.env`, and travels only inside the projected URL —
never as a standalone bootstrap field. The pooler's userlist carries the
matching entry; the gateway's own processes keep dialing the main identity.

The redis ACL user is added live at `ava start` (`ensure_cluster_redis_acl`), scoped to
the cluster's pub/sub channels (`ava:*`); it is re-affirmed on every start (not persisted
to redis.conf) and by the `redis-acl` gateway-watchdog healthcheck, so a redis restart
that drops the in-memory ACL is repaired before agents reconnect. Provisioning uses that
instance's own `default` user (the independent Redis admin password). A legacy `.env`
whose redis_url carries no username (`redis://:<runtime-password>@host/0`, born before the
names-as-data ACL model) dials as that `default` user — no ACL identity exists to
drop, so the healthcheck warns and skips rather than raising every round, and `ava
start` converge backfills the username into the URL (from the db_url identity) so the
cluster adopts the scoped ACL user. **Postgres and PgBouncer bind loopback + this
host's reachable address (`AVA_MACHINE_HOST`, default `localhost`), de-duplicated**
(never all interfaces): a single box resolves to loopback alone, while a split node
sets its real private-network IP, which is appended, plus the `scram-sha-256`
`AVA_TRUSTED_CIDRS` pg_hba ranges. Redis is the exception: it always binds
loopback-only, and the host-level `com.ava.redis-bridge` relay (`/usr/bin/python3
relay.py`) serves non-loopback Redis inbound by forwarding the host's
private-network address and Redis port to `127.0.0.1`. Each per-cluster pg is
started with
`max_connections = 500` (each agent process holds ~4 steady conns), passed on the
`pg_ctl start` line; pg_hba is written into `$AVA_HOME/pg/pg_hba.conf` and —
when the server is already running — reloaded (SIGHUP) so the rewritten hba takes
effect immediately instead of at the next restart (install-time birth starts pg
before the cluster's `.env` exists, so the first `ava start` rewrites it with the
real posture; Task #1113). `ava start`
is a *consumer*: it skips the bring-up when this cluster's pg/redis are already up
(`pg_isready` + a redis PING), and on a fresh start Postgres (and PgBouncer) first
waits (bounded, ~60s) for the reachable bind address to appear on an interface — so
a reboot that starts `ava` before the private-network interface exists retries rather
than dying on an un-bindable address. Redis never waits because loopback is always
available. pg/redis are never touched when already up, so a (re)start never
disrupts a running data plane — with ONE deliberate exception: a running
pgbouncer that answers on loopback but is missing its reachable-address listener
(a silently degraded double bind, task #1288) is RESTARTED rather than reloaded,
because a SIGHUP reload never
retries a `listen_addr` that failed to bind at startup. `ava stop` tears this cluster's
own instance down (data persists on disk); `ava cluster update` keeps it up (`--keep-infra`) for
the migrate step.

`repo` here is the checkout the running `ava` belongs to (resolved from where its `cli` source
lives, `cli/commands/_repo.py:_repo_root`), **not** the current directory — so a given `ava` always
targets the same cluster no matter where you run it. `install.sh` bootstraps the `ava` symlink into
`~/.local/bin/ava` (a .env-free bash step, so it works on a fresh host before secrets are filled);
the converge phase re-ensures it — and applies the rest of the host wiring — on every
`ava start` / `ava cluster update`. That global `ava` always means prod. For dev, run
`.venv/bin/ava` inside the worktree (which resolves the worktree's own `ava`).

The converge phase (`cli/commands/_converge.py:converge_host`) is idempotent — run
by `cmd_start`, so `ava cluster update` re-applies it on every upgrade. One gateway
`ava cluster update` converges the whole fleet through the Phase B fan-out. Run it standalone
with `ava converge`. It covers the `ava` symlink (re-ensuring install.sh's bootstrap),
`~/.local/bin` on PATH, the `$AVA_HOME` dir skeleton, and one prod-host integration for
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
brand-new host it first runs at `ava start`, not during `install.sh`.
On a gateway host it also registers the **fleet UI gate** (`cli/commands/_converge_gate.py`)
— a launchd KeepAlive job on macOS or user-systemd unit on Linux that owns the entry port (:3000) and proxies the Next.js app
on :3001. That step **replaces the running job only when the desired supervisor definition actually
changed** — checkout path, ports, or **the gate's own code and static assets**, which the
definition carries as a content hash of `services/gate/` (`AVA_GATE_CONTENT_HASH`; no reader,
its whole purpose is to move when the gate does). A rollout that touches none of the three
leaves the gate process alone and the entry never blinks; one that rebuilds the login page
or edits `daemon.py` replaces the job, which is the only way that change takes effect —
the daemon reads its pages into memory once at boot, so a new page on disk behind a running
gate is not deployed. Linux requires a running user manager, with lingering for
unattended boot; it stops the old unit before replacing the definition and
restarts crashes automatically. See [Linux gate supervision](linux-gate-supervision.md).
On macOS, when it does have to swap, it waits for launchd
to forget the old job before loading the new one — `bootout` returns before a draining job
is gone, and bootstrapping into that window fails with `Bootstrap failed: 5: Input/output
error`, which on 2026-08-01 left :3000 with no listener for the rest of the rollout. A load
that still never lands now **raises**, so the start / rollout fails instead of reporting
success over a dark entry port.
**A rollout does not update built-in schedule scripts.** The `schedules` table is
authoritative and boot-time provisioning only inserts rows that are missing, so a changed
template in `schedules/` reaches a running cluster only through an explicit
`ava schedules update <name> --script-file <template>` (which relaunches an enabled
schedule on the spot) — see [`schedules/README.md`](../schedules/README.md).
The schedule runner PROCESS is the exception: a code-change rollout bounces every live
`ava-schedule-<id>` session after the gateway boots, so runners (and the script text
they materialized at launch) always run the new checkout (Task #1746).
On agent-runners it also runs capability preflights: a headed Chrome (when the browser
is enabled, see below), a **probe of the configured cross-machine transfer backend**
(`AVA_CROSS_MACHINE_TRANSFER_BACKEND`, `drive` by default), and **the ability to open
and merge pull requests on the memory pool repo** (the nightly memory consolidation
runs `gh` + `git push` on each machine; the gate `_ensure_github_pr` →
`shared/github_pr.py:github_pr_blocker` fails loud unless `gh` is installed,
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
(`_ensure_cross_machine_transfer` → `shared/google_drive.py:find_writable_google_drive`)
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

Converge also carries a one-shot file-name migration: the durable
`--disable-service` marker used to be `$AVA_HOME/skipped_services` and is now
`disabled_services`, so a set recorded before that rename was silently unread and the
services it named came back on. Every converge promotes a leftover `skipped_services`
to today's name and logs what it moved plus the resulting disabled set; once promoted
there is no legacy file left, so later runs are no-ops. If BOTH names exist, the
current name stays authoritative — it is the name the code writes, so it is the
operator's later word, an empty file included ("nothing disabled" is a real value) —
and the legacy file is kept as `skipped_services.superseded` with both sets named in
the log, so two disagreeing files are visible instead of one being erased. Note that a
bare operator `ava start` (no `--disable-service`) rewrites the marker to empty by
design, so it re-enables the migrated set on that same start; internal restarts
(`ava cluster update` / recovery / `ava restart`) and the watchdog's 60 s round honor it.

**The cluster pin is behind the DB schema.** `[ops.schema] ... this checkout IS the
cluster pin ...` at ERROR, every round, with `ava cluster status` showing the whole
roster's DB-dependent services down, means the pinned commit does not carry migrations
the DB has applied. The watchdog deliberately stops here rather than self-healing: its
`ava cluster update` would move HEAD forward and the pin controller would force it straight
back, and nothing advances the pin because advancing it is a step of a *successful*
update. Preserve the target, applied migration set, lease/holder, and failure
evidence before choosing an authorized forward recovery or schema-compatible
rollback through the installed `ava cluster` CLI. Check that version's help and
recovery-point requirements first. Do not reset the production checkout, call
internal pin setters, or mutate migration state to make these facts appear aligned.
If the installed CLI cannot recover the mismatch, stop and report that exact
blocker; a manual source/pin change is not a substitute for a verified rollout.
Only recover a stranded pause after proving no live orchestrator owns it.
([decision](../decisions/2026-07-31-two-healers-must-not-own-the-same-checkout.md))

A paused posture with no owner — no *executing* update lease anywhere and no
orchestration session on the host — now self-clears in ~2 min rather than 10
(`STRANDED_PAUSE_TIMEOUT_S`, which is now sized for that case alone). One an
orchestration or a local `ava-updater` still owns is declined however long it runs; a
crashed holder stops being a live lease at its TTL, and a hung `ava-updater` is killed
by the stalled-updater reaper, so both become unowned rather than waiting on a second
timer.

A **settle hold naming this host** is the one lease shape that is not ownership, and it
is the shape a rollout leaves on an agent-runner whose updater died (a `POLL_STALLED`
host is folded into the hold). Nothing executes under a settle hold, so treating it as
an owner deadlocked the host: the pause gate blocks the whole round, so the pin and code
controllers — which have their own settle-hold exception, and whose heals produce the
convergence the hold is waiting for — never ran, and the hold could only lapse on
`SETTLE_TTL_S` (900s) while the watchdog's ERROR escalation fired at ten rounds. The
pause recovery now asks `DeployLease.awaits()` too, so such a host unpauses at the
2 min bound and converges on the following round
([decision](../decisions/2026-07-31-a-settle-hold-does-not-own-the-pause-it-waits-on.md),
issue #1116). **Symptom to recognise:** a host reading `waited-on` in `ava cluster
status` whose watchdog log repeats `round blocked by pause`. A hold naming a *different*
host still owns this host's pause, as does a lease with no note (a rollout executing
right now).

A **rollout that stops making progress** is reclaimed by the gateway watchdog
(`ops/controllers/stalled_rollout.py`) rather than waiting for a human. Every layer
above asks whether the lock holder is *alive*; this one asks whether it is getting
anywhere, because on 2026-08-02 a rollout hung 67 minutes inside `converge` on a
`codesign` child waiting for a GUI authorization no detached session can answer — the
holder pid stayed alive throughout, so stranded-pause, the resurrected watchdog, the
health probe and `ava cluster recover` all correctly stood down while the cluster sat
stopped. A rollout whose `cluster_last_update` row reads `RUNNING` and which has
written nothing to its `rollout-<epoch>.log` in `NO_PROGRESS_TIMEOUT_S` (900s, the
family's one no-progress number) is **interrupted, not killed**: the reclaim sends
`SIGINT` to the pid its own deploy-lease holder names (`<machine>:pid<N>`), so the
`finally` in `cli/commands/update.py` runs the recovery that was always there —
unpause this host, resume every paused agent-runner, record the terminal outcome,
leave the pin where it is. Only if that changes nothing for a further full window is
the session force-killed, and from there recovery falls back to the lease lapsing at
`LOCK_TTL_S` and the stranded-pause path above. **Symptom to recognise:** the rollout
log ends mid-phase and the next lines are the abort banner, with
`[ops.rollout] rollout … interrupting pid N` in the gateway watchdog log. If instead
you see `force-killed session ava-rollout`, the orchestration's own abort did **not**
run and the cluster is stopped + paused until the lease lapses — `ava cluster recover`
is the faster path. A healthy Phase B is not mistaken for this: the poll writes a
`still polling Phase B (Nm)` heartbeat on the lease-renewal cadence, so no phase of a
working rollout is silent for anything close to the bound.

Commands in the "long-running processes" / "E2E tests" sections below default to cwd = `$AVA_HOME/source/` (prod context). Dev work goes through `~/Ava/.worktrees/<task>/`.

## $AVA_HOME, installed packages, and node capabilities

The `$AVA_HOME` directory tree, the `PluginsConfig` / `installed.json` schemas,
the `ava plugins` / `ava skill` / `ava mcp` command surface, and the
capability -> service model are structure, not procedure. They live in the OKF
nodes co-located with their code:

| What | Node |
|---|---|
| `$AVA_HOME` layout, what derives from the home | `shared/paths/paths.ava.okf.md` |
| plugin enable config (`plugins_config.json`) | `shared/plugins_config.ava.okf.md` |
| `installed.json` schema, installable shapes, the scanner gate | `shared/install_registry/install_registry.ava.okf.md` |
| `ava plugins` / `skill` / `mcp` verbs, MCP merge layers, secret channel | `cli/commands/packages.ava.okf.md` |
| machine name, capability set, `machines` table, spawn-target 400 invariant | `shared/machine.ava.okf.md` |
| which services each capability contributes | `services/services.ava.okf.md` |

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
the process supervisor, `shared/posixproc.py`); orchestration sessions (updater /
rollout / cluster-restart) are native sessions on the same backend. Agent
interactive shells / watchers each run in their own detached pty host
(`shared/pty_sessions/` — one `pty.fork()` `bash -l -i` + pyte screen capture +
byte transcript under `$AVA_HOME/logs/` per host, session ops over the
session's own socket at `$AVA_HOME/run/pty/<name>.sock`; hosts reparent to
init at creation, so they are in no service roster and survive every stop /
update / respawn — a session ends only via its own kill, its shell exiting,
or a machine reboot).
A host runs the enabled service specs for its capabilities and runner mode,
not every row in this reference table. In particular, hosted mode must not
launch the process-mode restarter, including during update unpause or recovery.
Verify the actual process/session inventory as well as the desired roster.

In **process mode**, the agent (`agent-{N}` below) is a detached native process (a non-interactive agent
talks over DB + Redis and logs to a file, so a PTY only cost the per-box `kern.tty.ptmx_max` ceiling that
used to bound agent count). It is spawned double-forked onto init by the native process supervisor
(`shared/posixproc.py` on POSIX, `shared/winproc.py` on Windows), tracked by `agents_meta.pid` + a session
record under `$AVA_HOME/run/sessions/`, and is **not** a shell pane. The agent's own persistent
shells (`ava.shell`, `…-agent-{N}-shell-{n}`) each run in their own detached pty host
(`shared/pty_sessions/`).
In hosted mode, a null per-agent PID is expected; verify agent-host membership,
claim progress, and turn events instead. A process reaper must not interpret
that null PID as a dead hosted agent.

Session names follow the pattern `ava-<service>` (composed by
`shared/cluster/derive.py:session_name()`; neither machine nor cluster is encoded —
per-home hosting scopes them: the `$AVA_HOME/run/sessions/` records for native ones, the PTY
supervisor socket for agent shells / watchers).

<!-- lint:roster-table -->
| Service (suffix)         | Runs                                     | Healthcheck |
|--------------------------|------------------------------------------|-------------|
| `agent-{N}`              | `<venv>/python -m agent --agent-id N` — a **detached, native** process (double-forked onto init by the native supervisor), one per agent, created by spawn_agent. Tracked by `agents_meta.pid` + a `$AVA_HOME/run/sessions/` record; stdout/stderr → `$AVA_HOME/logs/agent-{N}.out.log`/`.stderr.log` | restarter watches local `agents.status='restarting'` + reaps dead pids (`agents_meta.machine = machine_name()` filter) |
| `gateway` ★ (gateway only) | `.venv/bin/python scripts/start_gateway.py` (FastAPI 0.0.0.0:8000) | `services.healthchecks.gateway` (HTTP `/api/agents` 200) |
| `ops` (agent-runner only) | `.venv/bin/python -m services.agent_ops.daemon` (inbound server on 0.0.0.0:<ops_port>; the gateway POSTs each cluster op to `/ops`, dispatched in-process via the gateway ops_* modules) | `services.healthchecks.ops` (`/healthz`) |
| `agent-host` (agent-runner only; **on the start roster by default — `AVA_RUNNER_MODE` defaults to `hosted` (2026-09-02); `process` is the explicit rollback opt-out)** | `.venv/bin/python -m services.agent_host.daemon` (the hosted runner: instead of one OS process per agent, one daemon runs every local agent's turns as asyncio tasks. One `PSUBSCRIBE` over every inbound channel replaces N per-agent subscriptions; a wake creates a turn task, the turn runs until the agent has nothing left to claim, and then the task ends — an idle agent is no task at all. Per-agent identity/config/plugin-config ride contextvars bound around each invocation, so one process serves many agents without a per-agent process. Bounds: `AVA_HOST_MAX_CONCURRENT_TURNS` (default 16, with a workload pool sized from it plus four connections of headroom), a separate fixed four-connection control pool for lifecycle and durable scans, and `AVA_HOST_AGENT_CACHE_SIZE` (32) + `AVA_HOST_AGENT_IDLE_TTL_SECONDS` (900) on the per-agent runtime cache. These are process-local psycopg client pools; PgBouncer is the downstream server-connection multiplexer and does not stop one client pool from starving its own borrowers. `GET /stats` on its health port reports cache hit/miss, turns started, wakes skipped, and who is running. **Restarting the host to clear ONE wedged agent takes down EVERY hosted agent on that runner** — their in-flight turns abort and resume from checkpoint, but it is a runner-wide interruption caused by one agent, and there is no per-agent kill: `asyncio.Task.cancel()` lands only at the turn's next await, so a turn blocked inside a C call cannot be killed short of the host (process mode's SIGKILL always worked). Before reaching for the restart, check the `host_turn_uncancellable` event — it names the agent, how long its cancel has been pending, and how long since its last completed LLM step. **Do not use the `/api/agents` `last_active_at` to judge whether an agent is wedged**: that field is `MAX(inbound_messages.created_at)`, not the real activity clock, and it goes stale during exactly the long turns where the question matters (issue #183) — a healthy heads-down agent reads as dead. Bounding this blast radius is issue #184. The lifecycle around the turns is hosted-aware: spawn/resurrect/restart/terminate deliver via wakes or in-process channels instead of forking/killing processes, and cluster update skips the per-agent drain (the agent-host service stop is the stop-the-world; in-flight turns checkpoint on SIGTERM) (`ops/ops_launch.py`, `ops/agent_wake.py`, `ops/ops_lifecycle.py`, `cli/commands/_update_quiesce.py`). On exclusive boot, an old applied hosted force is observed only when its agent has no persistent disposable-exec request envelope; surviving evidence leaves recovery deferred for inspection. The process-mode restarter is gated off on a hosted cluster — see `future/infra/agent-runner-as-server.md`) | `services.healthchecks.agent_host` (`/healthz` :8114) |
| `page-server` (agent-runner only) | `.venv/bin/python -m services.page_server.daemon` (supervisor of page servers: every open `agent_pages` row whose serve_dir is set — `ava.ui.serve()` pages — gets exactly one detached page server process on this host, spawned from the row's serve_dir on the row's port; rows that close get their server killed, while serve() pages stay open across an agent terminate. Truth source is the `agent_pages` table, not the session tree — a rollout's session rebuild does not kill page servers, an agent restart does not orphan them) | `services.healthchecks.page_server` (`/healthz` :8112) |
| `labeler`                | `.venv/bin/python -m services.labeler.daemon` (auto label generation) | `services.healthchecks.labeler` (`/healthz` :8103) |
| `im-bridge`              | `.venv/bin/python -m services.im_bridge.daemon` (IM frontends: Telegram; WeChat iLink / Feishu adapters shipped but **production-disabled since 2026-08-06** — `AVA_IM_DISABLED_ADAPTERS=weixin,feishu`) | `services.healthchecks.im_bridge` (`/healthz` :8111) |
| `heartbeat` (gateway only) | `.venv/bin/python -m services.heartbeat.daemon` (every `AVA_HEARTBEAT_INTERVAL_SECONDS`, default 15 min, scans `idling` agents past `AVA_HEARTBEAT_IDLE_THRESHOLD_SECONDS` that have not called `ava.self.pause_heartbeat()` and INSERTs a `heartbeat` check-in inbound; cluster-wide — the inbound-insert trigger wakes the agent on any machine, so it runs once on the gateway, not per agent-runner) | `services.healthchecks.heartbeat` (`/healthz` :8107) |
| `delivery-watchdog` (gateway only) | `.venv/bin/python -m services.delivery_watchdog.daemon` (two jobs on a fast tick, default 0.5s per `AVA_DELIVERY_WATCHDOG_INTERVAL_SECONDS`: **(1) wake dispatch** — re-publishes the Redis wake (with the wake-key breadcrumb) for every `pending` inbound of an `idling` owner older than `AVA_DELIVERY_WATCHDOG_DISPATCH_THRESHOLD_SECONDS` (default 1s), collapsing the lost-publish recovery from the claim loop's 30s recheck to ~1.5s; constant ~2 qps load, independent of fleet size; **(2) stall alerting** — WARNINGs chat inbounds still `pending` past `AVA_DELIVERY_WATCHDOG_THRESHOLD_SECONDS` (default 30s) whose owner is `idling`/`terminated`, once per row while stuck, with a `delivery_stalled` event emitted to the unified `events` stream. `running` owners are never dispatched or alerted (mid-turn queues are normal). Gate cluster-level on/off with `AVA_DELIVERY_WATCHDOG_ENABLED`) | `services.healthchecks.delivery_watchdog` (`/healthz` :8110) |
| `task-maintenance` (gateway only; **registered by the `ava_fleet` plugin**, not core — see `ava_builtins/plugins/ava_fleet/services.py`) | `.venv/bin/python -m ava_builtins.plugins.ava_fleet.task_maintenance.daemon` (every `AVA_TASK_MAINTENANCE_INTERVAL_SECONDS`, default 5 min, reminds owners of overdue in-progress tasks past their `remind_interval_seconds` window via a `chat` inbound; after `AVA_TASK_ESCALATE_N` (default 3) unanswered reminders, notifies the parent task's owner. Cluster-wide, runs once on the gateway. Discovered whenever the `ava_fleet` plugin code is present; gate its cluster-level on/off with `AVA_TASK_MAINTENANCE_ENABLED`) | `ava_builtins.plugins.ava_fleet.task_maintenance.healthcheck` (`/healthz` :8108) |
| `events-maintenance` (gateway only) | `.venv/bin/python -m services.events_maintenance.daemon` (every `AVA_EVENTS_MAINTENANCE_INTERVAL_SECONDS`, default 1h. Each pass incrementally maintains the Since-Birth day-grain rollups — `agent_metrics_daily` / `agent_model_tokens_daily` (the durable token+cost ledger) — from **Loki** (the unified event stream's live store): one union-family count probe compares retained candidate days with `rollup_day_state`; missing, failed, count-changed, and the latest `AVA_EVENTS_ROLLUP_LATE_WRITE_LOOKBACK_DAYS` (default 1) get a full-day overwrite, while clean days avoid the fourteen aggregate queries. The scan clamps to Loki's 168h retention floor (an outage longer than retention loses those days' Loki aggregates — logged loudly; the filtered `events-YYYYMMDD.rollup.jsonl` mirror (90-day retention by default, tunable via `AVA_EVENTS_JSONL_ROLLUP_RETENTION_DAYS`) then automatically repairs older ledger-watermark gaps: zero-known-row files fail loudly and are not counted as replayed, missing files remain unrecoverable; pre-LGTM history was backfilled once by the llm-cost-rollup-columns migration from the frozen PG archive), uses its own capacity-one Loki budget, and stops between days at `AVA_EVENTS_ROLLUP_PASS_DEADLINE_S` (default 1200), leaving untouched/failed state for the next pass. Today is served live by the readers (whole-life cost = ledger + Loki tail from the watermark). Full-day overwrite upsert keyed on the PK ⇒ idempotent; a zero-row indexed slice preserves existing ledger rows and marks the day failed for retry. Cluster-wide, runs once on the gateway — it owns the data plane. The rollup, JSONL replay, checkpoint pruner, blob vacuum and hourly checkpoint size sample are unconditional — the PG `events` archive slices (partition rolling, retention, index governance) were removed with the task #1281/#1823 cleanup; the table-drop migration (20260829T030000_drop-events-archive) is pending deployment. The daemon also hosts the per-thread checkpoint pruner (every 60s, newest three regardless of liveness) and the blob vacuum) | `services.healthchecks.events_maintenance` (`/healthz` :8109) |
| `restarter` ★ | `.venv/bin/python -m services.restarter.daemon` — runs three per-tick controllers over this host's agent rows. **RespawnController** (`ops/controllers/respawn.py`): restart dispatch, plus the dead-birth reaper for unclaimed `idling` rows past `boot_reap_grace_seconds`, the boot-phase reaper for dead `running`/`idling` rows that have produced no message, and the direct revive pass for dead post-message rows. Every reaper is machine scoped. **CrashResurrectController** (`ops/controllers/resurrect.py`): brings back involuntary deaths (`terminated` with `termination_source IN (reaper, launch-confirm)`) when work waits, subject to backoff. **WedgedAgentController** (`ops/controllers/wedged.py`): kills + resurrects live agents that stop consuming pending work; it uses a short idling threshold (`AVA_WEDGED_IDLING_AGENT_INBOUND_AGE_SECONDS`, default 180s) but preserves the long running-turn threshold. A `terminated` row retaining a live lease and pending terminate inbound is instead identity-reaped without resurrection, preserving the user's termination. Gated off the roster in hosted mode (`AVA_RUNNER_MODE=hosted` — per-agent process supervision is retired; see the `agent-host` row). | `services.healthchecks.restarter` (`/healthz` :8102) |
| `milvus`                 | `.venv/bin/python -m services.milvus.daemon` (`milvus-lite server` gRPC :19530, data dir `~/.ava/milvus-data/`) | `services.healthchecks.milvus` (TCP probe :19530) |
| `memory-indexer`         | `.venv/bin/python -m services.memory_indexer.daemon` (watchdog fs watch `~/.ava/memory/` + Gemini Embedding 2 → milvus collection) | `services.healthchecks.memory_indexer` (`/healthz` :8105) |
| `memory-search`          | `.venv/bin/python -m services.memory_search.daemon` (uvicorn on 127.0.0.1:19531 serving the exact-search store — in-memory matrix + npz persistence; the gateway and the indexer call it over HTTP when `AVA_MEMORY_SEARCH_BACKEND=numpy`) | `services.healthchecks.memory_search` (real POST /search probe :19531) |
| `frontend`               | `cd ui/web && NEXT_PUBLIC_GATEWAY_PORT=<AVA_GATEWAY_PORT> npm run build && npm run start -- -p <app_port>` (Next.js prod build, **loopback-only bind** (`next start -H 127.0.0.1`); off-box browsers reach it only through the fleet UI gate on the entry port `:3000` — see Private-network deployment. The build-time port is injected from `AVA_GATEWAY_PORT` so the browser dials the gateway on the right port even when it is not the default 8000) | `services.healthchecks.frontend` (curl) |
| `pg-backup` (gateway only) | `.venv/bin/python -m services.backup_scheduler.daemon` (cluster-clock daily dump schedule with bounded retry; after the Sunday 03:00 successful dump, runs one isolated logical restore drill; `/healthz` reports last-success age) | `services.healthchecks.pg_backup` (identity-verified `/healthz` :8116) |
| `pitr-uploader` (gateway only, `AVA_PITR_ENABLED`) | `.venv/bin/python -m services.pitr.uploader_daemon` (single-worker immutable GCS upload; WAL-only ciphertext staging is capped at 64 MiB and reported alongside spool bytes; disabled by default) | `services.healthchecks.pitr_uploader` (identity-verified `/healthz` :8117) |
| `pitr-base-candidate` (gateway only, `AVA_PITR_BASE_BACKUP_ENABLED`) | `.venv/bin/python -m services.pitr.base_scheduler_daemon` (weekly unprotected base candidate; when the additional `AVA_PITR_RESTORE_PROOF_ENABLED` gate is true, runs one generation-pinned isolated proof in the first-day 06:00 cluster-time monthly window when a candidate is pending; both default off) | `services.healthchecks.pitr_base_backup` (identity-verified `/healthz` :8118; candidate and restore-proof states are separate non-readiness-gating components) |
| `gateway-watchdog` ★ (gateway only) | `.venv/bin/python -m services.watchdog.daemon --role gateway` (asyncio imports + runs the gateway-capability healthchecks above — redis-acl first (re-affirms the cluster's redis ACL user (the identifier its redis_url carries), which a redis-server restart silently drops), then pgbouncer (restarts the per-cluster pooler when its listener stops answering OR its reachable-address listener is missing — a silently degraded double bind, task #1288; when the pooler is enabled it is every consumer's AVA_DB_URL, so it comes before any service that would be revived without a database), then gateway/im-bridge/labeler/heartbeat/delivery-watchdog/events-maintenance/milvus/frontend/pg-backup/otel-collector/task-maintenance/memory-indexer — every 60s). Its distinct `/healthz` reports the last completed tick and becomes stale after a 90s unfinished round. | the OS-scheduled **watchdog probe** (`ava cluster watchdog-probe --role gateway`, launchd / crontab / schtasks, every 60s) respawns it when its pidfile shows it dead |
| `agent-runner-watchdog` ★ (agent-runner only) | `.venv/bin/python -m services.watchdog.daemon --role agent-runner` (asyncio imports + runs the agent-runner-capability healthchecks above — ops/restarter (+browser, browser-mcp) — every 60s). Its distinct `/healthz` reports the last completed tick and becomes stale after a 90s unfinished round. | the OS-scheduled **watchdog probe** (`ava cluster watchdog-probe --role agent-runner`, launchd / crontab / schtasks, every 60s) respawns it when its pidfile shows it dead |
| `browser` (agent-runner only, auto-detect display; opt-out `AVA_BROWSER_ENABLED=false`) | `.venv/bin/python -m services.browser.daemon` (headed real Chrome, dedicated profile `~/.ava/chrome-profile/`, CDP :9222) | `services.healthchecks.browser` (HTTP probe `/json/version` :9222) |
| `otel-collector` | `<otel-collector-dir>/otelcol-contrib --config <otel-collector-dir>/config.yaml` (native Go binary installed by converge on the `lgtm-host` gateway and pure runners; unmarked gateway homes skip it; the gateway fans out only its cluster's labeled resources, pure runners relay with bearer auth; traces mirror locally; trace/log queues are file-backed while metrics use bounded memory) | `services.healthchecks.otel_collector` (valid empty OTLP POST must return 2xx on :4318; both :4318 and :8888 holders must resolve to this collector binary and its live session record, otherwise only a verified stale same-binary holder is reclaimed) |
| `browser-mcp` (agent-runner only, gated with `browser`) | `.venv/bin/python -m services.browser.mcp_daemon` (one shared `chrome-devtools-mcp` upstream attached to the headed Chrome, multiplexed over a Unix socket `~/.ava/chrome-mcp.<cdp_port>.sock` to every agent's chrome bridge — serial, with per-connection page affinity so one Chrome client is shared instead of one per browser-using agent) | `services.healthchecks.browser_mcp` (Unix-socket `list_tools` probe) |
| `computer-mcp` (agent-runner only, platform-gated: signed permissions helper enabled + capable, AF_UNIX transport, non-Windows host — Windows is the phase-3 pilot) | `.venv/bin/python -m services.computer.mcp_daemon` (computer-use executor: every desktop action through the signed permissions helper — serialized machine-wide, screen-coordinated (lease + FIFO queue + `release_control`), Vision OCR on snapshots, audited as `computer_action` + `computer_session_start/end` events, served over `~/.ava/run/computer-mcp.sock`) | `services.healthchecks.computer_mcp` (Unix-socket lock-free `ping` probe) |
| `mcp-daemon` (agent-runner only) | `.venv/bin/python -m ava._mcps_daemon` (ONE shared MCP daemon per machine, serving every agent over `~/.ava/run/mcp_daemon.sock` — sessions isolated per client connection, replacing the old one-daemon-per-agent children) | `services.healthchecks.mcp_daemon` (Unix-socket `ping` probe) |

The gate preserves the browser `Host` while proxying to the loopback frontend,
so the frontend CSP derives the same host that its API client uses. A TLS or
reverse proxy before the gate must overwrite `X-Forwarded-Host` and
`X-Forwarded-Proto` with the public browser origin; the gate relays those
headers only when present. Use lowercase `http` or `https` for
`X-Forwarded-Proto`; the frontend normalizes other casing before deriving its
CSP origin. The gate also relays the frontend CSP and static browser-security
headers to the public response.

The base-candidate gate additionally requires `AVA_PITR_REPLICATION_DB_URL`, a
local URL for a dedicated `LOGIN REPLICATION NOSUPERUSER` role. The role must be
accepted by the cluster's private loopback `pg_hba.conf`; its password must not
be embedded in commands or logs. This foundation does not create that identity
or enable PostgreSQL archiving. Until the activation runbook provisions and
verifies it, keep `AVA_PITR_BASE_BACKUP_ENABLED=false`. Candidate success never
replaces or prunes the daily and migration-bearing pre-update logical dumps.

Physical PITR is disabled by default. Converge only prepares
`$AVA_HOME/physical-backup/{spool,ack}` (0700) and atomically publishes the
self-contained shim at `$AVA_HOME/runtime/pg-archive/archive-shim`; it does not
set `archive_mode`, restart PostgreSQL, upload to GCS, or replace the daily and
pre-update logical dumps. Do not set `AVA_PITR_ENABLED=true` until the GCS
uploader and verified base-chain rollout have landed. Spool hard-bound failures
make PostgreSQL retain WAL in `pg_wal`; monitor their combined disk usage and
never delete unacknowledged segments to relieve pressure.
The uploader's seekable staging contract is restricted to bounded WAL files;
base backups must use the separate restartable streaming contract delivered by
the base-chain rollout and must never materialize a full ciphertext sibling.

`AVA_PITR_STORE_BACKEND` selects the object-store backend for the whole PITR
plane (default `gcs`); the other supported values are `baidu` (Baidu
Netdisk) and `oss` (Aliyun OSS). An unrecognized value fails fast at store
construction — a typo never silently falls back to GCS. Switching backends
is one env var + a restart; the previously retained copy stays primary until
the switchover runbook has been executed and observed. The full cut-over
procedure (restore drills, migration script, rollback): see
`conventions/pitr-backend-switchover.md`.

Activation is Ava-owned: use `ava cluster pitr status`, then
`ava cluster pitr activate --origin operator:<name>`. The command validates the
disabled shadow posture, creates the mandatory verified logical recovery floor,
persists every side-effect intent, applies archive settings with `ALTER SYSTEM`,
and dispatches the existing whole-cluster restart orchestration. Its typed,
non-secret continuation automatically resumes the exact operation after restart
readiness, executes `pg_switch_wal()`, and requires
`pg_stat_archiver`, the fsynced local ACK, and viewer-only exact
generation/size/CRC metadata to agree within the persisted five-minute deadline.
It then forces one operation-scoped base candidate and exact isolated restore
proof; only that chain may reach `protected`. That artifact is pinned while active;
terminal runs use a bounded two-artifact retention window.
Resume and status reverify that artifact. Preparation serializes with cluster
update/maintenance, binds the live PGDATA/port/postmaster/system-id identity on
both sides of the dump, and persists failures without resetting `started_at`.
Its credential check proves distinct service-account emails, not merely distinct keys. The
viewer proves object-list/read access without requiring bucket-metadata access; the
objectCreator + objectViewer uploader is identity-checked without creating or deleting a probe. Never edit PostgreSQL or `.env` manually
during this sequence. Resume a pending restart through the command; do not call
`pg_ctl` or introduce another restart mechanism. `ava cluster pitr rollback`
persists rollback intent, restores the frozen settings, disables PITR and
retention gates, and uses the same durable whole-cluster restart continuation.
Each of the four owned PostgreSQL settings has its own intent and applied
journal entry: resume distinguishes pre-ALTER from post-ALTER/pre-journal
failure without replaying a completed setting. Rollback restores only those
owned fields, so unrelated concurrent `ALTER SYSTEM` keys survive; readiness
verifies the owned semantic baseline after restart instead of requiring the
whole `postgresql.auto.conf` file to equal its old bytes.
It preserves logical dumps, local ACKs, remote objects, and protected manifests.

Restore proof additionally requires
`AVA_PITR_RESTORE_GCS_CREDENTIALS_FILE`, a distinct 0600 viewer-only service
account file; configuration rejects the uploader and viewer paths when they
resolve to the same inode. Before enabling the gate, prove the viewer cannot
create, overwrite, list-latest, or delete objects. The drill performs one
generation-pinned GCS download per object, then authenticates and extracts the
base locally under `$AVA_HOME/physical-backup/restore/`. Insufficient space
defers protection; do not reduce the WAL/spool, logical-backup, or emergency
reserves to force a run. A candidate remains `protected=false` until the real
isolated replay, promotion, fingerprints, live-Postgres identity check, and
immutable proof publication all succeed. Keep daily and pre-update logical
dumps regardless; this boundary has no retention or remote-delete operation.

`AVA_PITR_RETENTION_PLANNER_ENABLED=true` adds only a local dry-run after the
restore-proof gate is enabled. The private canonical plan lives at
`$AVA_HOME/physical-backup/retention-plans/latest.dry-run.json`; inspect its
digest, blockers, object counts and byte totals with:

```bash
ava pitr retention inspect
```

A blocked plan exits 2 and always has zero eligible objects. The flag grants no
delete credential, calls no remote delete API, does not alter Cloud Storage soft
delete, and leaves daily/pre-update `pg_dump` retention unchanged. Eligibility
also remains zero while any candidate is unprotected, while the plan is stale,
or while timeline history ancestry has not been authenticated. A WAL/history
object is continuous only after its local ACK and viewer-only remote inventory
entry match exactly on canonical archive/object path, generation, size, CRC32C,
and immutable metadata; any missing, extra, duplicate, or conflicting entry
blocks the complete plan.

All sessions have cwd set to the prod path `~/.ava/source/` (see "Prod and dev clone paths" above).
Session commands run under `bash -lc` (#476) — the login-shell flag pulls in the user's
`~/.bash_profile` / `~/.profile` so `~/.local/bin` (where `uv` typically lives on WSL / Linux) is on
PATH without needing a sudo-installed symlink. macOS dev hosts already inherit login-shell PATH from
Terminal.app; the change is load-bearing only for Linux / WSL agent-runners.

Agent process sessions are named `ava-agent-{N}` (N == agent_id) — the record/session
name `ops/agent_launch.py:_launch_agent_process` keys by; all daemon services follow the same
`ava-<service>` convention (`shared/cluster/derive.py:session_name()`). Agent processes, daemon
services, and agent shells / watchers are native-style sessions, so none of them are shell
panes; enumerate live agents via the native-supervisor records
(`native_proc().list_sessions()`) or `agents_meta` — both surfaced in `ava status` /
cluster status (the `agent_count` field) — and agent shells via
`python -m shared.pty_sessions.cli list`. `ava cluster status` enumerates
every live session — services, orchestration (updater / rollout / cluster-restart),
agent processes and shells. Raw session stdout is queried in Loki (see
Logging / diagnostics below).

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
enabled schedule as the current desired state for a later replacement. The
next agent boot reaps every preceding-generation watcher row and retains it as
`reaped` history; its prior watcher cron declaration is not automatically
restored and must be declared again. A reaped `at` or `launch` one-shot sends
the owner a missed notification. This applies to an inspection-only freeze too:
it is not safe to assume that existing desired-state sessions keep running after
the freeze acknowledgement.

Resume with the exact token printed by the freeze that this operator owns:

```bash
ava pty resume <generation-token>
```

A stale token cannot clear a newer freeze. `freeze`, `status`, and `resume` are
local host operations and remain usable while the gateway, Postgres, or Redis
is unavailable. A malformed marker fails closed; inspect the marker path shown
by `ava pty status` and perform an audited manual repair rather than treating
corruption as an implicit resume. Do **not** delete the marker: that changes the
current generation to `None` and can make the next watcher reconcile reap every
generation-bound declaration. Recover the original generation UUID from any
known-live PTY session record, rebuild a valid marker with that exact UUID, and
only then allow reconciliation to resume. If no record establishes the UUID,
leave the marker fail-closed and restore desired state only after an operator
has made the boundary explicit.

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

The Codex launcher in `ava-use-claude-code-and-codex` owns one canonical
session per `(cluster, workspace, tool)`. Check it before starting work:

```bash
python ava_builtins/skills/ava-use-claude-code-and-codex/reference/spawn_codex.py \
  /absolute/workspace \
  --tasks-file /absolute/workspace/tasks.md \
  --work-file /absolute/workspace/work.md \
  --status
```

A live record is adopted across agent changes instead of launching a duplicate.
Each ownership generation has a private
`$AVA_HOME/run/coding-tools/codex/<workspace-key>/<generation>/` state
directory and a fresh numeric PTY identity. `CODEX_HOME` points there and is
seeded only with the required authentication and configuration snapshot; no
mutable Codex database, session log, or transcript is shared between
generations. A rebuilt worker derives context from the workspace task file,
work log, collaboration contract, and Git state.

The launcher starts a quiet supervisor for the ownership generation. It closes
the full Codex PTY and terminalizes the record when `work.md` reaches `DONE` or
`HANDOFF`, the owner agent terminates, the Codex session crashes, the task
expires, or an operator cancels that exact generation. The default task TTL is
four hours and can be changed with `--ttl-seconds`; TTL is the fallback, not
the normal lifecycle boundary. Cancel only the generation printed by the
launcher or `--status`:

```bash
python ava_builtins/skills/ava-use-claude-code-and-codex/reference/spawn_codex.py \
  /absolute/workspace \
  --tasks-file /absolute/workspace/tasks.md \
  --work-file /absolute/workspace/work.md \
  --cancel-generation <generation-token>
```

A stale generation token cannot terminate a replacement owner.

### Agent-stack warm-up at start

After launching the service sessions, `ava start` fires a detached, best-effort
warm-up on every **agent-runner-capable** host
(`cli/commands/_warmup.py:_launch_agent_warmup` → `.venv/bin/python -m agent.warmup`,
logged to `$AVA_HOME/logs/warmup.log`). On a freshly-booted box the *first*
agent process pays a cold cost the rest do not: it cold-imports the heavy boot
chain (langgraph + the chat model + the ava SDK), cold-spawns its MCP daemon
subprocess, and — when the shared browser is on — does the one-time
`chrome-devtools-mcp` npx download plus a first CDP handshake. `agent.warmup`
does that work once, eagerly, so the OS page cache (and the npx package cache)
are warm before any real agent spawns.

It is **not** a service session: a one-shot process that exits, never healthchecked
or respawned. Every step is independently best-effort — a warm-up failure is
logged and swallowed, so a broken warm-up can only fail to *help* (the first
agent then pays the cold cost itself, the tested status quo), never break a
spawn or `ava start`. It is fire-and-forget: `ava start` does not block on it, so
an agent spawned within the warm window still races the cold path. This warms
the agent boot chain only; it does not affect the unclaimed-idling-to-claim window (the
row is claimed early, before the heavy import — see `agent/__main__.py`).

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
2026-08: the shared MCP daemon (`ava/_mcps_daemon.py`) dials the daemon's socket
directly (`"shared": "browser"` in the chrome `.mcp.json`, in-daemon line client
`ava/_mcp_browser.py`) — the former per-agent stdio bridge
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
  paths (watchdog respawn, boot autostart, `ava cluster update` rollout) never prompt and
  always take the fresh default; a host with no daily Chrome degrades silently to
  fresh.
- **On by default with auto-detect**: `AVA_BROWSER_ENABLED` defaults to true.
  On a headless machine (no `$DISPLAY` / `$WAYLAND_DISPLAY` on Linux), the
  converge step prints a warning and skips the browser — `ava start`
  proceeds normally without it. On a headed machine (macOS / Linux with display),
  the browser session and watchdog healthcheck engage automatically. Set
  `AVA_BROWSER_ENABLED=false` to explicitly opt out. The display verdict is
  computed consistently across processes: `$DISPLAY` / `$WAYLAND_DISPLAY` are
  passed through both env builders (`shared/session_env.py`) —
  forwarded into every daemon service session, and carried in the detached agent's
  inherited env dict (`agent_spawn_env_dict`) — so the watchdog and the agent see
  the same display the operator's shell does. Without this a headed Linux / WSLg
  host would strip the display and wrongly skip (and never revive) a browser it
  can actually run.
- **Capability-gated at two layers, observably**: (1) `_services_for_roles`, the
  watchdog's `_checks_for_capability`, and `agent/warmup.py` all gate on
  `browser_incapability()` (`shared/platform_probes.py`) — the single source of
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
  `ava start` (or let the next watchdog round revive the session once the port is
  free) — and the refusal message now names that remedy itself. When the squatter
  is one of *ours* (a Chrome left outside the session by a `SingletonLock`
  handoff), `ava stop --stop-browser` sweeps it, so there is no pid hunt: it kills
  every Chrome running on this cluster's `--user-data-dir`. A Chrome on some other
  profile is deliberately left alone — it cannot be positively identified as ours,
  and the operator's own browser is the thing that must never be killed — so that
  one is still quit by hand.
- **Login state preserved across stop / update**: the profile holds expensive
  hand-acquired logins, so an in-place `ava stop` and every `ava cluster update` /
  restart leave the `ava-browser` session running (`_do_stop` skips it
  by default, `keep_browser=True`) — a backend bounce never re-pops the window or
  risks a session-restore prompt; `ava start` is skip-if-running, so it leaves the
  live session alone. Only a full cluster teardown takes Chrome down: `ava stop
  --stop-browser`, and `ava cluster destroy` (its internal stop passes
  `--stop-browser` so a destroyed cluster leaves no orphan Chrome). Those two
  paths also sweep by profile after the session kills, because a Chrome that
  handed off is no longer in the session's process tree
  (`services/browser/orphan.py`).
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
  respawning it. If the wait marker cannot be written, the probe and healthcheck
  use the same bounded read-only readiness check instead. The gate never
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
Next.js frontend. Agent memory accounting depends on the configured runner mode:
process mode has a separate agent process; hosted mode shares an agent-host
runtime and does not require a per-agent PID. Several layers bound the footprint:

- **Hosted runner (the flagship layer)** — the end state the fleet is moving
  to: one `agent-host` daemon runs every local agent's turns as asyncio tasks,
  and an idle agent is **no task at all** — identity lives in `agents_meta` +
  its checkpoint. An idle agent's ~36MB resident heap + MCP daemon + pooled
  Postgres connections + Redis subscription all go to zero between wakes by
  construction, not by swap-out machinery. Phase 1 (dispatcher, turn tasks,
  hosted-aware lifecycle) is built behind `AVA_RUNNER_MODE=hosted` (now the
  default; `process` is the explicit rollback opt-out); the historical hibernation layer (controller, `hibernating`
  status, SIGUSR1, `/hibernating` endpoint, `AVA_HIBERNATE_*` keys) was
  DELETED in 2026-08 — its design docs stay for history:
  [`decisions/2026-07-20-agent-hibernation.md`](../decisions/2026-07-20-agent-hibernation.md),
  [`future/infra/agent-runner-as-server.md`](../future/infra/agent-runner-as-server.md).
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
  [`agent/db/db.ava.okf.md`](../agent/db/db.ava.okf.md).

None of this claims memory stops mattering — it is the honest current floor.
The next walls once memory is handled: the heartbeat's
wake-rate ceiling (~1.67/s, ≈750 agents on today's numbers) and LLM turn cost,
which is linear in fleet size regardless of any of the above.

### Start / check / restart

Day-to-day ops **only uses the `ava` CLI** — one line to bring up the full set / check status / stop everything:

```bash
ava start [--machine-name X --serve-gateway --serve-agent-runner --memory-remote URL --gateway-url URL]
             # Bring up an already installed/enrolled home; start does not birth it.
             # Enabled services depend on capabilities and runner mode, not a fixed count.
             # Pure bring-up: the home comes from the checkout-anchored boot (never the cwd, never a flag — identity is the path). An uninstalled home fails fast pointing at install.sh / install.sh --worktree / ava enroll by role. First run on a fresh install still takes --machine-name (+ --gateway-url if the install didn't write it); serve flags come from the install's --role via .env. idempotent
ava memory init
             # After a new install, explicitly create the memory checkout and seed its template. A split agent-runner bootstraps from its running gateway. Start, update, and rollback never initialize or validate the memory repository.
ava status   # one screen of sessions / probes (http / tcp / pid); filtered by role (agent-runner skips the pg/redis probe). A gateway host also gets a `gate (fleet UI entry)` section: whether anything answers on the entry port (:3000) and whether this cluster's own supervisor still holds the gate (launchd label / Linux user-systemd unit). ANY HTTP status counts as answering — the gate serves 503 (updating) and 502 (app rebuilding) by design, so a 2xx test would call a correctly-working gate dead mid-rollout; it is liveness-only for the same reason the frontend row is (the entry port is the public bookmark and carries no identity payload), which is what the supervisor line covers. Both halves also ride in `ava cluster health-probe`'s check 5 — alert-only, never feeding auto-rollback, since a gate that failed to register is not a code regression and no rollback puts it back. On any installed host warns if the prod source `$AVA_HOME/source` has drifted off `main` (an agent that developed in the prod tree instead of a worktree) — it runs the live cluster, so a feature branch there means un-reviewed code live + force-discarded on the next rollout. Also shows the cluster pin (`cluster_target_sha`, the commit the last rollout pinned the cluster to) vs this host's HEAD, so a node that missed a rollout is visible (read-only; fail-fast-on-drift is future — see commit-pinned-cluster.md)
ava stop     # kill of the service sessions via the session backend (graceful SIGTERM to the top process, tree SIGKILL on timeout) + reap this cluster's agent sessions (ava-agent-<id>: SIGTERM so each agent tears down its MCP daemon / Claude Code subprocess, then force-kill stragglers — they are spawned via the gateway, not ServiceSpecs, so nothing else stops them; agent shells/watchers (per-session pty hosts) are force-killed) + stop this cluster's own native pg/redis (gateway: pg_ctl stop + redis shutdown, data persists on disk; agent-runner skips); stdin y to confirm. First best-effort POSTs /api/cluster/stopping?machine=<self> so the cluster view shows this host as "stopped" (intentional) not "offline" (crash) — the live probe can't tell them apart. Stays local by design: it kills the gateway + its own data plane, which can't be delegated to the gateway
# Read AVA_CLUSTER_SECRET without echo and export it for this command first; unset it afterward.
ava enroll --gateway URL --machine-name NAME --machine-host HOST   # join a split-deployment agent-runner to a gateway (presents AVA_CLUSTER_SECRET to the gateway's authenticated /api/bootstrap without putting it in argv); --machine-host is this runner's reachable address (written to the $AVA_HOME/machine_host file, so a re-enroll keeps it) — the gateway dials its ops server there; verifies the runner projection, which every runner process re-fetches at startup, then run `ava start`. Add --health-port-base N (a block base on the allocator's grid, 18000 + k*22, e.g. 18110; pick one no local cluster already owns — `ava cluster ls`) only when another Ava unit shares this machine's localhost namespace — daemon health ports are a per-UNIT fact the gateway does not serve, and two units otherwise take the same defaults 8102-8109. A WSL2 host auto-applies its own reserved base when the flag is omitted (issue #1152), since a co-located native Windows unit would otherwise default to that same shared block; a base already in `.env` — hand-set or auto — survives a bare re-enroll
ava cluster update   # thin client on every host: POST /api/cluster/rollout to the gateway, which spawns the three-phase orchestration in a detached `ava-rollout` session running `ava cluster update --local` and returns 202. `--local` is the explicit in-process escape hatch used by detached orchestration sessions and for debugging. Every in-process leg (`--local`, including `--local --restart-only`; the agent-runner self-update; and `ava restart`) refuses to run from inside an ordinary supervised session — a pty-hosted agent shell, agent process, or service daemon — because its own stop leg kills that session's whole tree, the orchestration with it (the 2026-08-12 stranding; `shared.proc.hosting_supervised_session`). The detached orchestration sessions are the narrow exemption. See "Multi-machine ava cluster update orchestration" below
ava cluster rollback [--to SHA|TAG] [--keep-pin] [--set-known-good] [-y]  # default is a cluster rollback: stop-the-world (local + remote restarters paused, fleet quiesced) -> gateway schema/code rollback -> pin write-back -> remote runner self-update with mode=none (the drain already happened) -> poll. A runner still converging holds the deploy lease for the settle window and receives a best-effort resume; its watchdog then converges it to the rewritten pin. `--keep-pin` is the deliberate gateway-only escape hatch: it skips pin write-back and runner fan-out, then resumes the paused runners on their unchanged pin. `--set-known-good` explicitly makes the rollback target the anchor
ava cluster status   # full multi-machine roster (thin client: GET /api/cluster/roster; gateway resolves its own row + probes each agent-runner by dialing a status_probe op to its ops server). online = live probe answered; stopped = machines.stopped_at set by `ava stop` (cleared by `register_self()` — on `ava start`, and on the ops daemon's own boot); offline = neither (crash / unreachable); STALE-STOP = the probe answered AND the stop marker is set, i.e. the two sources of truth disagree — the marker is the wrong one (a latch, cleared only by a `register_self()`), and until it is cleared the `ava cluster update` fan-out, which filters on that same marker, would drop this host; the next rollout reconciles it (`_resolve_fanout_targets`). The `up since` column is that same `register_self()` stamp (`machines.up_since_at`) — a boot/announce time, never a heartbeat: nothing refreshes it while a host merely keeps running, which is why `online` is the live probe and not a freshness test on this column. The `pin` column shows each node's HEAD vs the cluster pin (`cluster_target_sha`): ✓ on-pin / ✗ off-pin (missed a rollout) / ? no pin yet or HEAD unknown — the gateway computes the verdict (`on_pin`) server-side so the roster stays a bare list. Same per-node pin view on the Control page; the `agent-runner-watchdog` tick also self-heals when this host is off-pin — `_check_pin_drift` force-updates it to the pin via a spawned update (`_spawn_update(target_sha=pin)`, cooldown-guarded, skips that tick's healthchecks; a paused host returns at the `_tick` gate first so it never fights a rollout). It also declines while ANY update is in flight on this host, not only while the cluster lease is held — being off-pin is a running update's own mid-flight state, and a watchdog-spawned `ava-updater` takes no lease, so the lease-only check let this force the checkout back underneath a live updater and flap prod between two commits for two hours ([decision](../decisions/2026-07-31-two-healers-must-not-own-the-same-checkout.md), issue #1074). If you see this dimension idle on a host that is off-pin, look for an `ava-updater` session before assuming the heal is broken. The `gateway-watchdog` only warns on an off-pin gateway (`_warn_gateway_off_pin`) — a gateway drift needs a rollout, not a single-host self-checkout. The `code` column is the separate question the pin cannot answer: it shows the commit the process that answered the probe froze at ITS OWN boot (`shared/process_sha.py`), so `⚠` there means the checkout moved but that process was never restarted — a node can read `pin ✓` and `code ⚠` at once. `ava start` will not clear it (it skips already-running sessions); `ava restart` will — and on an agent-runner the `agent-runner-watchdog`'s **code controller** (`ops/controllers/code.py`) now spawns exactly that restart on its own once HEAD is on the pin but the running commit is not. It is the second half of the pin dimension: `_check_pin_drift` heals *off*-pin hosts, so an on-pin host running old code (a Phase-B checkout whose restart declined — the 2026-07-28 wsl state) used to be off every controller's map and needed a human. Same guard set as the pin heal (agent-runner only, declines while the cluster update lock is held or a local orchestration session is alive, persistent per-commit backoff, shared process cooldown), and it never runs outside the prod source tree — with one exception, which is why a `pin ✗` or `code ⚠` host beside a `waited-on` cell now clears in a watchdog round instead of on the hold's TTL: a **settle hold naming this host** is not a deploy mutating the cluster, it is a stated waiting period whose content is "this host has not converged", and nothing executes under it, so deferring to it made the hold and the heal wait on each other (`DeployLease.awaits`, issue #1020). The pin heal takes the same exception for the same reason — `settle_hosts_converged` will not release until the named host reports BOTH `head_sha == pin` (pin's dimension) and `running_sha == pin` (code's). A lease with no note — a rollout executing right now — still defers, as does a hold naming someone else. The stranded-pause recovery takes that same exception (issue #1116) rather than deferring to any live lease: unpausing is not itself convergence, but a paused host cannot reach the heals that are, because the pause gate blocks the whole round ahead of them — so a host that is both paused and waited-on was the one host forbidden to do the thing the hold was waiting for. Per-daemon granularity is on each daemon's own `/healthz` `sha`. The `hold` column is the third dimension, and the only one that is not a probe: it is transcribed from the live `cluster_update_lock` lease (`deploy_hold` = the lease sentence, stamped cluster-globally onto every row and printed once as a banner above the table; `waited-on` = this host is named in a settle hold's note). It answers "why was my deploy refused", which until now was legible only in the health-probe cron log or by reading the lock row by hand — but it answers ONLY that: `waited-on` is the hold's recorded set, not a live convergence verdict (`pin` / `code` are), a blank cell is "not named by the hold" and not "converged" (a host that never acked is never named), and a blank column is not proof no deploy is running, because a watchdog-spawned host-local `ava-updater` takes no lease at all. The roster deliberately reads the lease row rather than calling `deploy_in_flight()`, which would probe every machine and *release* a converged hold — neither belongs on a status GET. Above both banners sits the **last-update banner**: a rollout writes its own outcome to `cluster_last_update` (`shared/last_update.py`) — opened before Phase A and closed in the orchestration's `finally` — and the roster states it when it FAILED, because the `pin` and `code` columns alone are symptoms several unrelated states share (a node that missed a rollout, a checkout moved without a restart, a rollout that failed and rolled back all read the same there). **Pin semantics on failure:** a failed rollout does NOT roll the pin back. If the gateway reached its target before failing, the pin advanced and the hosts still off it converge via their own watchdog; if it failed earlier, the pin was never moved and nothing is converging toward the failed target — the banner says which. A rollout whose orchestration is killed never closes its row, and that is read as `orphaned` once its deploy lease lapses (the record is written ahead of the work precisely because the dying process cannot file it). A successful `ava cluster update` replaces the record, so nothing has to remember to clear a failure. The banner also carries the **rollback anchor** (`cluster_pin.last_known_good_sha`, surfaced beside the pin on both the roster and the status page): it was recorded since the pin existed and shown nowhere, so a rollback used to present as the pin simply moving to an older commit. And when an external observer has acted on the failure — today `ava cluster rollback`, which the health probe's `--auto-rollback` shells into — it writes what it did onto the record (`observed_by`, e.g. `rolled back 8bdd366 -> 7e571b4`) without touching the verdict: the dying orchestration files nothing, but the process cleaning up after it witnessed the death, and that sentence is what makes the surfaced failure actionable rather than merely visible. A failure the cluster has already come back from reads `recovered` rather than `aborted`, which is a different call to action — nothing to repair, only something to know, so both surfaces mark it as a warning instead of a failure. It is reached from either side: the orchestration writes it when its own gateway leg rolled back to last-known-good (rc=1 on the pull path), and a reader derives it for any failed record carrying an `observed_by`, which is how a rollback that cleaned up after a dead orchestration closes the 2026-07-30 story. Finally, the record names the rollout's own log (`log_path`), threaded down as `ava cluster update --rollout-log` by the `spawn_rollout` that created the file — so the banner points at THE log rather than at the `$AVA_HOME/logs/rollout-<epoch>.log` glob. It is stamped once, by the intent write, and no later writer touches it; a foreground `ava cluster update --local` is not teed to a file and records nothing, since naming an older rollout's log would be worse than naming none
ava cluster restart  # bounce the WHOLE cluster (this host + fan out to agent-runners, no git pull) via POST /api/cluster/restart. The gateway's detached `ava-cluster-restart` session runs `ava cluster update --local --restart-only`; plain `ava cluster update --restart-only` is also a thin POST, never the detached child's command. `ava restart` is the local single-host form
ava cluster ls       # list all registered clusters in ~/.ava/clusters.json
ava cluster down --path PATH   # stop the cluster at a home path (its gateway + its own pg/redis instance), keeping its registry slot + data dirs (the safe way to stop a dev worktree cluster from another checkout)
ava cluster destroy --path PATH [--drop-db]   # stop a cluster + free its registry slot (port block) + deregister its OS-scheduled jobs (health probe, both watchdog probes, autostart); --drop-db also removes its pg/redis data dirs; refused for the default home ~/.ava
```

`ava cluster update --dry-run` resolves and validates the target, runs the
non-disruptive runner fetch and prepare checks, and reports the predicted
maintenance window. It creates no recovery snapshot, pause/stop state, pin,
or checkout; it is the operator check before a real rollout. The gateway runs
it in the detached `ava-rollout-dryrun` session, outside the orchestration-kind
scan, so a live dry-run does not block or get interrupted by a real rollout; a
dry-run still refuses while a real orchestration is live, and a second concurrent
dry-run is rejected by the session backend's duplicate-name guard. Prepare-check
failures return a failing verdict, while the maintenance estimate is informational
and never permits or refuses a rollout. The estimate uses
the p95 of the ten most recent stop-the-world, local-leg, and readiness stages.
Phase B remains recorded and shown in the breakdown, but is excluded because it
begins after readiness, while the gateway is serving, and measures remote-runner
convergence rather than the maintenance window. Offsite publication of a real
rollout's verified local snapshot runs detached only after recovery/finalization,
so it is outside the maintenance window and cannot delay resumption.

Down-failure drill: see [down-failure-drill.md](down-failure-drill.md).

**Rollback health guard and observation window.** `ava cluster health-probe` retries
gateway liveness three times, 30 seconds apart, before declaring it unhealthy. A
failed liveness or agent-population check is classified against the data plane:
Postgres/Redis reachability failure is environment-class, alerts the owner, and never
increments the auto-rollback counter; a healthy data plane with a failing gateway,
population, crash-loop, or schema check is code-class and counts normally. Any gating
failure, including a deploy-suppressed or environment-class one, restarts the pending
known-good observation streak. A backend rollout writes its target as
`pending_known_good_sha` while preserving `last_known_good_sha`; two consecutive
gating-pass probes after at least 10 minutes promote that target to the rollback anchor.
Rollout completion is therefore not itself proof that the new commit is known good. If
an `INCOMPLETE` Phase-B rollout self-heals onto that same target, this authoritative
promotion finalizes its last-update record as `RECOVERED`, retaining the original
failure detail while making clear that there is nothing left to repair.

**`ava start`'s exit code means something.** Three outcomes, because "the start
sequence ran" and "this host is serving" are different facts:

| rc | Meaning | What to do |
|---|---|---|
| `0` | every step ran and every **critical** service passes its liveness probe (non-critical services get a 45 s window and then stop blocking the start — a straggler is reported and alerted, not a failure) | nothing, unless the printed verdict names non-critical services that missed their window — an alert was posted for those |
| `4` | every step ran; a **critical** service never passed its probe inside `SERVICE_READY_TIMEOUT_S` (180 s) | read the snapshot printed just above — the failing rows are named, with the session list repeated after the snapshot. The host is **up but incomplete**: the watchdog keepalive is already retrying, `ava status` re-checks, and `ava start` is idempotent to run again |
| `1` | a start *step* failed (converge, the data plane, migrations, the schema assertion, machine registration) | the host may have no services at all; the failing step printed why |

The gated set is derived, not listed: it is `ops/spec.py`'s roster for this host's
capabilities, minus anything `_gate_reason` skips (`browser` with no display,
`browser-mcp` with no AF_UNIX, a disabled `heartbeat`), minus
`--disable-service`, minus the frontend (whose ~30-60 s build would otherwise set
the floor for every gateway start). **A service that is skipped is not a service
that is unready** — it never reaches the gate and cannot fail a start. A service
with no probe at all (`browser-mcp`, whose transport is a Unix socket only its
healthcheck dials) likewise cannot: absence of evidence is not failure.

**The gate is tiered** (Task #2183, C2): the critical roster
(`cli/commands/_probe.py:CRITICAL_SERVICE_SESSIONS` — gateway / frontend /
restarter / agent-host / im-bridge / the two watchdogs; CTO ruling: critical =
a failure cuts user-visible core function or the ops safety net) is the only
one that can fail a start and the only one waited on for the full 180 s. Every
other launched service gets a 45 s window
(`shared/deploy_timing.py:NON_CRITICAL_SERVICE_READY_TIMEOUT_S`) and then stops
blocking the start; one that missed the window is printed as a cross AND posted
to the alerts store (the same channel the health probes use), so the downgrade
is a verdict change, never a silence. The alert is one instance per service,
reused while the failure stays open, resolved by the next start that finds the
service up, and its IM push is suppressed under `--no-readiness-gate` (the
boot job's uncapped retry must not spam the user's IM). This is the
2026-08-30 rollout's lesson: its local start spent 182 of 197.5 s on a
pitr-uploader healthz whose failure nothing downstream depended on.

`ava start --no-readiness-gate` keeps the wait and the printed crosses but exits 0
anyway. Two callers pass it and an operator normally should not:

- **the boot job**, on every platform (`ava boot`'s child argv, and the macOS
  autostart plist's `ProgramArguments`). Its retry has **no attempt cap** by
  design, so a non-zero exit means retry forever — and a box whose headed Chrome
  will never launch would re-run `ava start` every 60 s while otherwise serving
  perfectly. launchd's `SuccessfulExit` is a boolean and cannot tell rc 1 from rc
  4, so opting out on *all three* platforms is the only way they keep the single
  retry behaviour `shared/boot_policy.py` requires. What the boot loop exists to
  retry — a start that failed before launching anything, e.g. a runner whose VPN is
  not up yet — still exits 1 and is still retried.
- **the rollout's local gateway leg**, because `_gateway_ready` asks the same
  question one step later and better (off-box, authenticated, through the probe
  each runner's preflight uses). See the deploy-window section.

**OS-scheduled jobs.** Three kinds go to the platform scheduler — the health
probe (`shared/os_cron.py`), one watchdog probe per capability
(`shared/os_watchdog_probe.py`), and the boot autostart (`shared/os_autostart.py`)
— as launchd LaunchAgents on macOS, crontab lines on Linux, `\Ava\<home-slug>\`
tasks on Windows (`shared/os_schtasks.py`). Two properties are load-bearing:

- **A job spec is anchored to the checkout that wrote it.** `ava_binary_path()`
  resolves this checkout's `.venv` binary (PATH only as a fallback), and the
  launchd plist / crontab line pin `AVA_HOME` explicitly, so a job registered by
  cluster X can never run cluster Y's `ava` against cluster Y's home. A Windows
  task action has no env slot and relies on the interpreter path alone.
- **A health probe never reloads its own launchd label.** Auto-rollback runs
  `ava start` below the health-probe LaunchAgent, and `launchctl bootout` would
  terminate that whole recovery process tree. Registration recognizes the
  inherited `XPC_SERVICE_NAME`, leaves the existing plist untouched, and lets
  the next external converge apply any pending spec change.
- **Windows task settings are stated, not inherited.** Registration goes through a
  task definition (`schtasks /Create /XML`, written to
  `$AVA_HOME/run/schtasks/<kind>.xml`) rather than `/Create` flags, because Task
  Scheduler's defaults stop every job when a laptop runner goes on battery and let
  one wedged invocation block its successors for 72 hours. The definition sets the
  power settings and a per-kind `ExecutionTimeLimit`; the boot job is the one that
  is deliberately unbounded. Details + the AC caveat:
  [`windows-setup.md`](windows-setup.md).
- **`AVA_OS_JOBS_ENABLED=false` disables registration for a process.** The
  scheduler is one namespace per OS user, so a test-scoped `$AVA_HOME` cannot
  isolate it — the pytest suite sets this and `tests/conftest.py` fails any run
  that leaves a job behind. Deregistration is never gated. Operators do not set
  this: a prod cluster with it off silently loses its health probe, its watchdog
  probes, and its ability to come back after a reboot.

Differences between `ava cluster update` and `ava stop && ava start`:
- **graceful stop** — the session backend signals the service (SIGTERM to the top process on native
  sessions; every service session is native), the daemon runs its cleanup
  (close DB pool / remove pidfile / flush log / drain HTTP); only on timeout does it fall back to hard
  kill (tree SIGKILL). `ava stop` defaults to hard kill, losing in-flight data.
- **coordinated pipeline** — use the installed `ava cluster update` implementation,
  its dry-run, recovery-point gates, and target-specific verification. Its internal
  checkout, dependency, migration, and service stages are not a manual operator
  recipe. A merged PR does not replace an already imported orchestrator; verify
  the running implementation before relying on a newly added safety gate.
- **No stdin confirmation** — runs through automatically, no prompt blocking. `ava stop` still asks for confirmation (ops needs to be explicit
  about "I want it stopped now" before taking the hard-kill path).


## Private-network deployment (phone / multi-device access)

The gateway binds all interfaces on the **gateway host** (both address
families); the Next.js app binds loopback only and is reachable **only through
the fleet UI gate** — the always-up entry on `:3000`, which itself binds all
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
authenticated route requires the cluster secret (`AVA_CLUSTER_SECRET`, presented
as a bearer token (agent-runner / `/api/bootstrap`
/ `/ops`) or a signed session cookie (browser login). See
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
configured gateway host at the frontend entry port; set the variable to a
comma-separated list to replace that derived allowlist. Cookie-authenticated
state changes also reject a present, non-allowlisted `Origin`. On a host where
another service holds the gateway port, set
`AVA_GATEWAY_PORT` plus the matching
`AVA_GATEWAY_HEALTH_URL=http://localhost:<port>/api/health`
(two-var contract; `ava status` probes the health URL). A pure agent-runner
derives this health URL from the reachable `AVA_GATEWAY_URL` written by
`ava enroll` unless the host sets an explicit health URL override.

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

`AVA_CLUSTER_SECRET` is the control-plane bearer only: `/api/bootstrap`,
`/ops`, gateway API requests, and machine registration. It is normally stable;
run [`scripts/rotate_cluster_secret.py`](../scripts/rotate_cluster_secret.py)
only after a bearer leak. Its dry-run preflights `GET /api/bootstrap` with the
current bearer and a rejected invalid bearer. Execute stages only the new bearer
in the gateway `.env`; restart that gateway, then push the bearer to every
enrolled runner and restart them. It does not change Postgres, Redis, ACLs, or
PgBouncer.

Routine data-plane rotation is independent and uses
[`scripts/rotate_data_plane_secrets.py`](../scripts/rotate_data_plane_secrets.py):

```bash
.venv/bin/python scripts/rotate_data_plane_secrets.py                 # dry-run, both scopes
.venv/bin/python scripts/rotate_data_plane_secrets.py --scope admin --execute
.venv/bin/python scripts/rotate_data_plane_secrets.py --scope runner --execute
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

## Code flow & Events

Kernel-side LLM calls go through `llm.astream()`; a LangChain callback publishes
chat / code / reasoning start + delta to the Redis `ava:events` channel on each
chunk. The full role table (payload fields, publisher, when each fires) is in
`shared/live_events.ava.okf.md`; interrupt semantics for cancel / terminate are in
`agent/graph/graph.ava.okf.md`.



## Observability / Tracing

The observability stack (user decision 2026-08-11, architecture task #1266):
**OTel + Tempo + Loki + Prometheus + Grafana**. The one gateway home carrying
`$AVA_HOME/lgtm-host` and every pure runner run an **OTel Collector sidecar**
(`ava-otel-collector`, supervised by the watchdog and installed by converge
from `deploy/otel-collector/`). Producers export OTLP/HTTP to their local
sidecar (`AVA_TELEMETRY_OTLP_ENDPOINT`, default `http://127.0.0.1:4318`; the
ingress port is one setting, `AVA_TELEMETRY_OTLP_PORT` — single source for the
sidecar receiver, the gateway's remote receiver and the port probes,
task #1945). An
unmarked gateway skips the collector and the default producer export, keeping
the JSONL event mirror only; an explicitly configured OTLP endpoint opts the
producer into that external collector. Delivery is role-specific: the marked
gateway collector writes traces to the Tempo selected by the host-scope
`AVA_TELEMETRY_TEMPO_ENDPOINT` setting (prod's override selects the remote WSL
Tempo) and logs/metrics to gateway-loopback Loki/Prometheus; a pure runner
collector keeps the same three exporter component IDs and relays each
signal to the gateway collector at the host from `AVA_GATEWAY_URL`, port 4318,
with `Authorization: Bearer $AVA_CLUSTER_SECRET`. The remote receiver binds
only the exact non-loopback `AVA_MACHINE_HOST`, never `0.0.0.0`/`::`; the
local receiver remains `127.0.0.1:4318` without auth. Combined single-box
deployments keep only the local receiver, including when their secret is set.
Every application log, metric and trace Resource carries `cluster` = this
home's display label. The gateway collector drops any non-null cluster that
does not match its own, while retaining null-cluster legacy/filelog/infra
resources. It fans out:

- **traces** → Tempo OTLP/HTTP (`AVA_TELEMETRY_TEMPO_ENDPOINT`, default
  `http://127.0.0.1:14318`; prod sets a host-scope override to the remote WSL
  Tempo) + local JSONL mirror
  (`$AVA_HOME/traces/spans.jsonl`, rotated `spans-<ISO>.jsonl`).
- **logs** — every unified event (the `events` table write path) dual-writes to
  OTLP logs (Loki) via `shared/telemetry_otlp.py` → sidecar → Loki
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
  duplicating the gateway's series. A gateway whose Postgres URL has an empty
  password omits the contrib Postgres receiver (which rejects an empty
  password) but keeps its unauthenticated Redis receiver.
- **collector delivery metrics** — every sidecar scrapes its per-unit loopback
  self-metrics endpoint every 30s into `metrics/infra`
  (`AVA_OTELCOL_METRICS_PORT`, default 8888). The local watchdog probes the same
  endpoint. Grafana rules alert on current queue pressure, new enqueue failures
  over 5m (counter delta, never lifetime absolute value), and a recently-seen
  machine whose collector stopped reporting for 5m. The watchdog logs current
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
reading per machine (`shared/resource_sample.py`) — the degraded answer for a
deployment whose LGTM backend is down or was never deployed — and link to the
Grafana host dashboard for the trend. The retired `shared/resource_monitor.py`
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
data. There is no Postgres fallback: `events` is a read-only archive. This is
one startup-applied kill switch, so a change requires a process restart. The
home/role producer gate additionally prevents an unmarked gateway from using
the default loopback endpoint; explicitly setting `AVA_TELEMETRY_OTLP_ENDPOINT`
bypasses that gate without creating a local collector.

**LGTM backend lifecycle** — home-scoped native launchd jobs on Darwin arm64
and user systemd units on Linux amd64 run Loki, Prometheus (GOMEMLIMIT
2GiB / 1GiB), and Grafana. Unit names include the home slug; Linux ownership
also checks the loaded unit file and exact executable. Explicit host listen
ports permit isolated homes; defaults remain 3100/9090/3003 plus Loki gRPC
9095. See [native lifecycle](../cli/commands/lgtm.ava.okf.md). Tempo is configured per cluster; prod's host-scope
override targets the remote WSL Tempo. No
service lifecycle depends on a container backend. The backend is required while the gateway serves /ops
and the inspect endpoints (consumers: the gateway Loki/Prometheus read paths,
ops alerting via Grafana's embedded Alertmanager → the gateway webhook, the
events-maintenance Loki rollup, `ava cluster health`). It is a **host
singleton** owned by the lifecycle on exactly one home per host — the
observability station. Provider identity is either the operator-created
`$AVA_HOME/lgtm-host` marker file (in practice prod `~/.ava`;
`touch ~/.ava/lgtm-host` once, or `ava lgtm on`) or the declarative
`observability-station` unit capability (`ava start
--serve-observability-station` / `AVA_MACHINE_SERVE_OBSERVABILITY_STATION` /
`$AVA_HOME/machine_serve_observability_station`). On the station home, converge
installs pins from `deploy/lgtm/native/versions.yml`, renders native configs
and native service definitions, and runs the idempotent lifecycle on every `ava start`
/ `ava cluster update`. The observation data volume is a per-machine knob:
`AVA_LGTM_STORAGE_DIR` (empty default = `$AVA_HOME/lgtm/native/data`) moves the
Loki filesystem store and Prometheus TSDB to a configured path. The gateway watchdog re-runs it when Loki/Prometheus/Grafana readiness
probes hit connection failures; its probe-first path skips only a reachable
backend whose matching launchd job is still loaded. `ava status` shows native
jobs and readiness probes.
**Loki config hard gate** — any change to the native Loki config (the
`deploy/lgtm/native/config/loki.yaml` template, which converge renders into
`$AVA_HOME/lgtm/native/config/loki.yaml`) must pass
`loki -config.file=<cfg> -verify-config` before the job is (re)started.
The launcher (`deploy/lgtm/start.sh`) runs that verification against the
rendered config on every invocation and **refuses to start Loki when it
fails** — a bad field name otherwise crash-loops the launchd job (the
2026-08-25 `ingester.wal_disk_full_threshold` incident, ~3min of Loki-backed
read downtime). Legacy compose assets are not a supported production rollback
procedure. Use the native lifecycle and its rendered configuration; do not
introduce a container backend to recover this deployment. The Python-side
pin (`shared/loki_index_labels.validate_loki_deploy_config`) still guards
converge-time re-renders; the binary verify is the zero-cost last line
before any restart, including the gateway watchdog's.
Unmarked homes (dev worktree clusters) never touch these backends or install a
gateway collector. Their gateway Loki readers reject the implicit loopback URL
with HTTP 503; explicitly setting `AVA_TELEMETRY_LOKI_URL` opts reads into a
caller-managed stack. Dashboard and fleet queries, as well as every Loki alert
rule, also filter `cluster` so a shared backend cannot leak another home's
telemetry into the result.
Deliberate stop: remove the marker or `ava start --disable-service lgtm`, then
`deploy/lgtm/stop.sh` — see `deploy/lgtm/README.md`.

**Recording is one collector hop from the producer** (sidecar architecture, task #1266). The
previous inline-POST design raised `Exception while exporting Span.` whenever
the POST failed; the agent-side mirror (record/ship split 2026-06-16) fixed
that but left the mirror as the only durable copy. When the home/role gate
allows export, recording is an OTLP export to the configured collector
(normally the local sidecar). Trace and log exporters use **file-backed sending
queues** (file_storage) with unlimited retry, so their accepted backlog
survives sidecar restarts. Metrics intentionally use a bounded in-memory queue
and a 15-minute retry window, then shed old points; cumulative instruments
repair their totals on a later successful sample. `shared/telemetry_otlp.py`
also sheds (counted) instead of blocking, so an unreachable sidecar never
touches the main write path. Exporter IDs stay `otlphttp/tempo`,
`otlphttp/loki` and `otlphttp/prometheus`; in particular, renaming Tempo/Loki
would orphan their file_storage backlog during an upgrade.

**Record** — `shared/trace.py:initialize_tracing`, gated by `AVA_TRACE_ENABLED`
(default **on**). Instrumentation is OpenLLMetry (`traceloop-sdk`); the sole span
exporter is `OtlpJsonHttpSpanExporter`, which POSTs each export batch as one
standard OTLP/JSON `ExportTraceServiceRequest` to the configured collector's
`/v1/traces` (JSON wire format; LLM content stripped before it leaves the
process). The sidecar's file exporter mirrors each batch line-for-line to
`$AVA_HOME/traces/spans.jsonl` — the durable, vendor-neutral, grep-able
source of truth (any OTLP backend ingests the same lines; rotation bounds the
directory by size/day/backups, and the agent-start prune enforces
`AVA_TRACE_RETENTION_DAYS` / `AVA_TRACE_MAX_DIR_MB` as the final guard). A
sidecar not answering at agent init reports once and starts one daemon retry
loop. Both the trace precheck and event exporter retry every five minutes; the
event exporter records disabled/recovered attempts as real events in the JSONL
mirror that survives the outage.

**Ship** — `ava trace ship` (`cli/commands/trace.py`). Recovery replay reads
the mirror and bypasses the LOCAL sidecar, because replaying through it would
write the replayed lines back into the mirror (watermark loop). A gateway or
single-box unit POSTs straight to loopback
`{AVA_TELEMETRY_TEMPO_ENDPOINT}/v1/traces` without auth; a pure runner POSTs
to the gateway collector's private port 4318 with the cluster bearer. The
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
`graph.ainvoke`, `agent/_runloop.py` opens `turn_span(name="ava-agent-N",
session_id=str(agent_id), turn=N)`, a native OTel root span stamped with the
neutral `session.id` (the viewer groups one agent's turns into a session by
it) and `ava.turn`. One trace = one turn: the root span closes and exports
when the turn's work is done. All child spans (LLM calls, tool execs,
retries) share that root's trace_id + parent; without the wrap each LLM call
is an orphan. Positioning: traces are a **drill-down tool for bounded units**
— a finished turn rendered as a waterfall. The primary observation surface
for long-running agents is the unified event river (the `events` table + its
Loki dual-write above), not Tempo.

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
| what did a *detached* `ava cluster update` child do | same two — the spawner exports `AVA_CLI_LOG_NAME` so the CLI wires the sinks |
| why did a daemon vanish | its log file: every daemon wraps `asyncio.run(main())` and logs the traceback before re-raising |
| what did milvus say | its log file only — it is a C++ binary with no PG sink |
| an agent | `$AVA_HOME/logs/agent-{N}.log` (kernel + its exec subprocess, both appending) |
| raw session stdout (gateway / shells / daemons / schedules) | Loki (the LGTM backend): shell logs → `filelog/sessions`; gateway/daemon/schedule logs → `filelog/services`; updater/rollout tees → `filelog/orchestration`. Banner-only agent main stdout is excluded. All filelog receivers derive Loki `service_name` from the filename and persist offsets. Loki retains 7 days; scheduled local cleanup uses the family tiers below. See `deploy/lgtm/README.md`. |

### Local log retention

`ava logs retention` removes expired files from the root of this checkout's
`$AVA_HOME/logs` only. Its allowlist is deliberately narrow: agent-main
`ava-agent-<id>.out.log`, named PTY
`ava-agent-<id>-shell-<n>-<name>.{out,host}.log`, and Loguru rotations named
`<service>.YYYY-MM-DD_HH-MM-SS_<pid>.log`. It never traverses subdirectories or
follows symlinks, and it skips every file held open by a visible process.

Preview the exact paths, UTC mtimes, sizes, and total bytes before deleting:

```bash
ava logs retention --dry-run
ava logs retention
AVA_LOG_RETENTION_DAYS=21 ava logs retention --dry-run
ava logs retention --older-than 21
ava logs retention --family-days agent=15,shell=7,gateway=30,ops=30,watchdog=30,other=3 --dry-run
```

The age is a positive integer number of days. `--older-than` and
`--family-days` are mutually exclusive. Without either flag, the legacy global
threshold remains: `AVA_LOG_RETENTION_DAYS`, otherwise 14 days. `--older-than`
is the explicit global override. `--family-days` activates the C baseline:
agent-main and rotated `agent-*` files 15 days; named PTY shell transcript/host
files 7 days; `gateway*`, `ops*`, and `*-watchdog` / `*_watchdog` rotations 30
days; all other allowlisted service rotations 3 days. The rotation shape also
admits underscores, so `delivery_watchdog` is in the watchdog family. Supply
only the family values that differ; omitted values retain that baseline. In a
mapping, `default=N` aliases `other=N` for the catch-all service family.

Dry-run candidates include their family and selected days, followed by one
`retention_family` line per policy family (including zero-candidate families)
with its candidate count, days, and bytes. A file exactly at its cutoff is
retained (`mtime < cutoff` is deleted). Delete failures are reported per path on
stderr, remaining candidates are attempted, and the command exits nonzero if any
inspection or deletion failed.

Register one low-traffic daily OS job per machine after deployment, using the
same launchd/crontab scheduling layer as `shared.os_cron`, with that machine's
anchored `ava` executable as the payload. Registration is deployment work; the
command intentionally does not add or mutate an OS schedule itself.

Use the following payload in the deployment-owned jobs; these are templates,
not instructions to register a job from the CLI:

```text
# macmini launchd — StartCalendarInterval: Hour=4, Minute=35
/Users/<user>/.local/bin/ava logs retention --family-days agent=15,shell=7,gateway=30,ops=30,watchdog=30,other=3

# mba launchd — StartCalendarInterval: Hour=4, Minute=40
/Users/<user>/.local/bin/ava logs retention --family-days agent=15,shell=7,gateway=30,ops=30,watchdog=30,other=3

# WSL crontab — 04:40 daily
40 4 * * * /home/<user>/.local/bin/ava logs retention --family-days agent=15,shell=7,gateway=30,ops=30,watchdog=30,other=3

# Windows schtasks — 04:45 daily; use the worktree's own executable
schtasks /Create /TN "\\Ava\\<home-slug>\\log-retention" /SC DAILY /ST 04:45 /TR "\"C:\\path\\to\\Ava\\.venv\\Scripts\\ava\" logs retention --family-days agent=15,shell=7,gateway=30,ops=30,watchdog=30,other=3"
```

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
legacy `agent_events` mirror), and the monthly partitioning are in `shared/log.ava.okf.md`.

## CI (Continuous Integration)

CI runs on **GitHub-hosted `ubuntu-latest` runners** via the workflows in
[`.github/workflows/`](../.github/workflows/): `ci.yml` (backend pytest +
pyright, frontend eslint + tsc + vitest, e2e Playwright happy path) and the
image/retention workflows. A fork gets CI for free — GitHub Actions provisions
the runners, no self-hosted infrastructure required. The test suite, migration
smoke, and e2e self-provision **throwaway native** pg/redis clusters per xdist
worker (`tests/_containers.py`: `initdb` + `redis-server` on ephemeral
127.0.0.1 ports, data dir on a tmpfs, torn down after), so a runner only needs
the toolchain (Python, Node, uv, Postgres + Redis server binaries; e2e also
needs Playwright chromium) — no Docker, no shared engine.

> The maintainer's deployment runs CI on a dedicated self-hosted runner fleet
> (per-job runner isolation for timing determinism) instead of the GitHub-hosted
> runners. That setup — host roster, provisioning scripts, the eval Docker
> image — is operator-specific and lives in their private deployment notes, not
> in this repo.
