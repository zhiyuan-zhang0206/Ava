# Runtime model

## Clusters, units, prod, and dev clone paths

A **cluster** = one logical deployment. Every cluster — including `main` — owns its
OWN Postgres + Redis instance (under its `$AVA_HOME`, on a per-cluster pg/redis
port), so co-located clusters share no data plane at all: isolation is
home-directory isolation, not a database name / redis logical-DB index / channel
prefix kept correct inside one shared instance. A cluster also owns one outward
gateway and a contiguous host-port block. (The rationale — and the remaining slice 3,
bundling the pg/redis binaries — is in
`future/infra/embedded-per-cluster-data-plane.md`.)
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
- **agent-runner** capability: hosts agents + the ops server + restarter; its
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
`cli/commands/_repo.py:session_name()`; neither machine nor cluster is encoded —
session hosting is host-local AND per-home: native session records under `$AVA_HOME/run/sessions/`
(services / orchestration / agent processes), pty session records + per-session
sockets under `$AVA_HOME/run/pty/` (agent shells / watchers), so the home
already scopes every session). Every cluster produces e.g.
`ava-gateway`; two clusters are two homes, never one namespace.

**CI is a separate hosting surface.** The CI image (`Dockerfile`) installs
its own session tooling inside runner images — an isolated surface that has
nothing to do with the cluster runtime described here. Changes to the
cluster's session handling never touch it, and its tooling never reaches a
cluster home.

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

Postgres and Redis run as native processes (no Docker — the binaries come from brew
on macOS / apt on Linux, but Ava drives them directly via `pg_ctl` + `redis-server`,
not `brew services`/launchd/systemd). Every cluster — including `main` — brings up its
OWN pair under `$AVA_HOME` on its per-cluster ports (`cli/commands/_cluster_instance.py`):
`initdb` into `$AVA_HOME/pg` (template-cached through a host-level dir beside the
registry, so a new cluster / a test spins up by directory copy rather than a fresh
multi-second init), plus `redis-server` with its data dir under `$AVA_HOME/redis`.
`ava start` ensures this cluster's instance is up and `ava stop` tears it down — there
is no standalone infra verb, and no shared host instance to survive across
checkouts/worktrees.

The data-plane posture is uniform — the default is multi-machine, a single box is just
the case where the reachable address is loopback (no single-vs-multi branch). **Auth is
always on, per-cluster**: each cluster authenticates to Postgres as its own role
and to redis as its own ACL user (the identifier its URLs carry), both with `AVA_CLUSTER_SECRET`
when one is set (an EMPTY secret — the single-box default — serves everything
unauthenticated on loopback: no scram, no requirepass, no bearer). The redis instance is single-tenant, so
its `requirepass` — the `default` admin user, used only to provision that ACL user — IS
the cluster secret; there is no separate box-level admin secret. pg loopback stays
`trust`, so the pg role password is only consulted once `AVA_TRUSTED_CIDRS` is declared.
Settings re-applies the db_url + redis_url password (the secret) on every
load, so a `.env` snapshot out of step can never reach the wire. On the same load, a
data-plane URL whose host is this machine's own reachable address (`AVA_MACHINE_HOST`)
dials `127.0.0.1` instead (`shared/config/data_plane.py`): self-dial never leaves the box
— it must not route through the NIC or a VPN's network extension, which can transiently
black-hole a self-connect to its own IP. The `.env` value, the verbatim bootstrap
payload, and the registered address stay untouched — only the in-memory dial host
changes, so remote runners keep dialing the gateway's real address.

**Least-privilege runner role** (`ava_runner`, Task #1236): runner processes do
not dial the main data-plane identity. A bootstrap fetch with `?role=runner`
returns `AVA_DB_URL` projected onto the fixed `ava_runner` role (LOGIN
NOSUPERUSER NOCREATEDB NOCREATEROLE), whose grants cover exactly the audited
runner surface: SELECT on every table (plus sequence USAGE), SELECT/UPDATE on
`agents_meta` (status/liveness), SELECT/UPDATE/INSERT on `inbound_messages`
(claim AND the agent-side self-lifecycle inbounds — `ava.self.terminate` /
`restart` / `compact` insert their own rows), SELECT/UPDATE on `agents`
(`ava.self.set_label` writes the agent's own row), INSERT/UPDATE/SELECT on
`machine_units` + INSERT/UPDATE on `machines` (register_self / mark_stopping
— `ava start` / `ava stop`), INSERT/UPDATE on `host_deploy_state`
(set_posture), INSERT/UPDATE/DELETE on `api_idempotency` (the runner's ops
server dedupes /ops calls), INSERT/UPDATE on `agent_tasks` (`ava.tasks`) and
`agent_watchers` (`ava.watcher`), UPDATE on `agent_pages` (page close at
exit), and full CRUD on the LangGraph checkpoint tables. `agents` INSERT,
`agents_meta` INSERT, notices writes, the cluster deploy-state tables and any
DDL fail under it by construction — the 2026-08-12 pollution class (full write
credential on the runner) is structurally impossible. Its password (`AVA_RUNNER_DB_PASSWORD`) is minted
at install (or by `ava cluster ensure-runner-role` on pre-cutover clusters),
kept in the gateway's `.env`, and travels only inside the projected URL —
never as a standalone bootstrap field. The pooler's userlist carries the
matching entry; the gateway's own processes keep dialing the main identity.

The redis ACL user is added live at `ava start` (`ensure_cluster_redis_acl`), scoped to
the cluster's pub/sub channels (`ava:*`); it is re-affirmed on every start (not persisted
to redis.conf) and by the `redis-acl` gateway-watchdog healthcheck, so a redis restart
that drops the in-memory ACL is repaired before agents reconnect. Provisioning uses that
instance's own `default` user (requirepass == the cluster secret). A legacy `.env`
whose redis_url carries no username (`redis://:<secret>@host/0`, born before the
names-as-data ACL model) dials as that `default` user — no ACL identity exists to
drop, so the healthcheck warns and skips rather than raising every round, and `ava
start` converge backfills the username into the URL (from the db_url identity) so the
cluster adopts the scoped ACL user. **pg/redis always bind
loopback + this host's reachable address (`AVA_MACHINE_HOST`, default `localhost`),
de-duplicated** (never all interfaces): a single box resolves to loopback alone, while a
split node sets its real private-network IP, which is appended, plus the `scram-sha-256`
`AVA_TRUSTED_CIDRS` pg_hba ranges. Each per-cluster pg is started with
`max_connections = 500` (each agent process holds ~4 steady conns), passed on the
`pg_ctl start` line; pg_hba is written into `$AVA_HOME/pg/pg_hba.conf` and —
when the server is already running — reloaded (SIGHUP) so the rewritten hba takes
effect immediately instead of at the next restart (install-time birth starts pg
before the cluster's `.env` exists, so the first `ava start` rewrites it with the
real posture; Task #1113). `ava start`
is a *consumer*: it skips the bring-up when this cluster's pg/redis are already up
(`pg_isready` + a redis PING), and on a fresh start first waits (bounded, ~60s) for the
reachable bind address to appear on an interface — so a reboot that starts `ava` before
the tailscale interface exists retries rather than dying on an un-bindable address. So a
(re)start never disrupts a running cluster's data plane. `ava stop` tears this cluster's
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
`~/.local/bin` on PATH, the `$AVA_HOME` dir skeleton, a gateway-host guard that fails
loud on frontend build-time env overrides (`ui/web/.env{,.local,.production,.production.local}`
bake `NEXT_PUBLIC_*` into the bundle and silently beat the runtime gateway inference —
the 2026-06-09 outage), plugin config images, the pre-rename disabled-services marker
carry-over (below), and each enabled plugin's `scaffold()` (`ava_memory`'s brings up the memory pool checkouts and seeds `MEMORY.md` + the commit-cap hook; a disabled plugin is skipped and leaves nothing behind). The unit-state steps (plugin images / plugin scaffolds) need a configured
unit, so on a brand-new host they first run at `ava start`, not during `install.sh`.
On a gateway host it also registers the **fleet UI gate** (`cli/commands/_converge_gate.py`)
— the launchd KeepAlive job that owns the entry port (:3000) and proxies the Next.js app
on :3001. That step **replaces the running job only when the desired plist actually
changed** — checkout path, ports, or **the gate's own code and static assets**, which the
plist carries as a content hash of `services/gate/` (`AVA_GATE_CONTENT_HASH`; no reader,
its whole purpose is to move when the gate does). A rollout that touches none of the three
leaves the gate process alone and the entry never blinks; one that rebuilds the login page
or edits `daemon.py` replaces the job, which is the only way that change takes effect —
the daemon reads its pages into memory once at boot, so a new page on disk behind a running
gate is not deployed. When it does have to swap, it waits for launchd
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
On agent-runners it also runs capability preflights that fail loud rather than letting
a missing capability surface later: a headed Chrome (when the browser is enabled, see
below), a **writable shared Google Drive folder**, and **the ability to open and merge
pull requests on the memory pool repo** (the nightly memory consolidation runs `gh` +
`git push` on each machine; the gate `_ensure_github_pr` →
`shared/github_pr.py:github_pr_blocker` fails loud unless `gh` is installed,
authenticated, and has write access to the pool repo). The Drive + GitHub-PR gates are
**split-deployment-only**: both are auto-skipped when this unit also carries `gateway`
(a single box has no peer to hand files to and consolidates memory locally), and a split
agent-runner that does not use them can opt out with `AVA_REQUIRE_GOOGLE_DRIVE=false` /
`AVA_REQUIRE_GITHUB_PR=false`. A host whose memory must stay on-box instead runs
`AVA_MEMORY_KEEP_LOCAL=true`: the pool becomes a local-only git repo (no remote, no
push / pull / PR), and the GitHub-PR gate is skipped regardless of role. The Drive gate
(`_ensure_google_drive` → `shared/google_drive.py:find_writable_google_drive`) is how
the fleet does cross-machine file transfer without a relay: every agent-runner mounts the
same Google Drive account, so an agent hands a file to another machine by dropping it in
its local Drive folder (the synced `My Drive` area — the mount root is not writable) and
passing the path; the peer reads it from its own Drive folder once it mirrors over. The
gate verifies participation with a write+read+delete round-trip, so a split agent-runner
with no signed-in Drive (or only an unwritable mount root) cannot `ava start` until Drive
is set up — unless it opts out (above) or is a single box. It probes the per-OS Drive
locations: macOS
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
update. Two ways out, both a human decision — advance the pin to a commit that carries
them (`shared.cluster_pin.advance_pin`, which a completed rollout also does), or roll
the schema back to what the pin carries (`shared.migrations.rollback_to`). Then `ava
cluster recover` if the paused posture is still set, and `ava start`. Advancing
the pin immediately after any manual `git reset` is mandatory for the same reason — the
pin controller undoes an un-pinned recovery within 60 s.
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
| `$AVA_HOME` layout, what derives from the home | `shared/paths.ava.okf.md` |
| plugin enable config (`plugins_config.json`) | `shared/plugins_config.ava.okf.md` |
| `installed.json` schema, installable shapes, the scanner gate | `shared/install_registry.ava.okf.md` |
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
A host runs the **union** of its capabilities' sessions: a host carrying only
`agent-runner` runs just the 3 marked with `★`; a `gateway` host runs the rest; a single-box
(`gateway,agent-runner`) host runs all of them.

The **agent process** itself (`agent-{N}` below) is the same detached native shape (a non-interactive agent
talks over DB + Redis and logs to a file, so a PTY only cost the per-box `kern.tty.ptmx_max` ceiling that
used to bound agent count). It is spawned double-forked onto init by the native process supervisor
(`shared/posixproc.py` on POSIX, `shared/winproc.py` on Windows), tracked by `agents_meta.pid` + a session
record under `$AVA_HOME/run/sessions/`, and is **not** a shell pane. The agent's own persistent
shells (`ava.shell`, `…-agent-{N}-shell-{n}`) each run in their own detached pty host
(`shared/pty_sessions/`).

Session names follow the pattern `ava-<service>` (composed by
`cli/commands/_repo.py:session_name()`; neither machine nor cluster is encoded —
per-home hosting scopes them: the `$AVA_HOME/run/sessions/` records for native ones, the PTY
supervisor socket for agent shells / watchers).

<!-- lint:roster-table -->
| Service (suffix)         | Runs                                     | Healthcheck |
|--------------------------|------------------------------------------|-------------|
| `agent-{N}`              | `<venv>/python -m agent --agent-id N` — a **detached, native** process (double-forked onto init by the native supervisor), one per agent, created by spawn_agent. Tracked by `agents_meta.pid` + a `$AVA_HOME/run/sessions/` record; stdout/stderr → `$AVA_HOME/logs/agent-{N}.out.log`/`.stderr.log` | restarter watches local `agents.status='restarting'` + reaps dead pids (`agents_meta.machine = machine_name()` filter) |
| `gateway` ★ (gateway only) | `.venv/bin/python scripts/start_gateway.py` (FastAPI 0.0.0.0:8000) | `services.healthchecks.gateway` (HTTP `/api/agents` 200) |
| `ops` (agent-runner only) | `.venv/bin/python -m services.agent_ops.daemon` (inbound server on 0.0.0.0:<ops_port>; the gateway POSTs each cluster op to `/ops`, dispatched in-process via the gateway ops_* modules) | `services.healthchecks.ops` (`/healthz`) |
| `page-server` (agent-runner only) | `.venv/bin/python -m services.page_server.daemon` (supervisor of page servers: every open `agent_pages` row whose serve_dir is set — `ava.ui.serve()`/`serve_markdown()` pages — gets exactly one detached page server process on this host, spawned from the row's serve_dir on the row's port; rows that close, or agents that terminate, get their server killed. Truth source is the `agent_pages` table, not the session tree — a rollout's session rebuild does not kill page servers, an agent restart does not orphan them) | `services.healthchecks.page_server` (`/healthz` :8112) |
| `labeler`                | `.venv/bin/python -m services.labeler.daemon` (auto label generation) | `services.healthchecks.labeler` (`/healthz` :8103) |
| `im-bridge`              | `.venv/bin/python -m services.im_bridge.daemon` (IM frontends: Telegram; WeChat iLink / Feishu adapters shipped but **production-disabled since 2026-08-06** — `AVA_IM_DISABLED_ADAPTERS=weixin,feishu`) | `services.healthchecks.im_bridge` (`/healthz` :8111) |
| `heartbeat` (gateway only) | `.venv/bin/python -m services.heartbeat.daemon` (every `AVA_HEARTBEAT_INTERVAL_SECONDS`, default 15 min, scans `idling` agents past `AVA_HEARTBEAT_IDLE_THRESHOLD_SECONDS` that have not called `ava.self.pause_heartbeat()` and INSERTs a `heartbeat` check-in inbound; cluster-wide — the inbound-insert trigger wakes the agent on any machine, so it runs once on the gateway, not per agent-runner) | `services.healthchecks.heartbeat` (`/healthz` :8107) |
| `delivery-watchdog` (gateway only) | `.venv/bin/python -m services.delivery_watchdog.daemon` (two jobs on a fast tick, default 0.5s per `AVA_DELIVERY_WATCHDOG_INTERVAL_SECONDS`: **(1) wake dispatch** — re-publishes the Redis wake (with the wake-key breadcrumb) for every `pending` inbound of an `idling` owner older than `AVA_DELIVERY_WATCHDOG_DISPATCH_THRESHOLD_SECONDS` (default 1s), collapsing the lost-publish recovery from the claim loop's 30s recheck to ~1.5s; constant ~2 qps load, independent of fleet size; **(2) stall alerting** — WARNINGs chat inbounds still `pending` past `AVA_DELIVERY_WATCHDOG_THRESHOLD_SECONDS` (default 30s) whose owner is `idling`/`hibernating`/`terminated`, once per row while stuck, with a `delivery_stalled` event emitted to the unified `events` stream. `running` owners are never dispatched or alerted (mid-turn queues are normal). Gate cluster-level on/off with `AVA_DELIVERY_WATCHDOG_ENABLED`) | `services.healthchecks.delivery_watchdog` (`/healthz` :8110) |
| `task-maintenance` (gateway only; **registered by the `ava_fleet` plugin**, not core — see `ava_builtins/plugins/ava_fleet/services.py`) | `.venv/bin/python -m ava_builtins.plugins.ava_fleet.task_maintenance.daemon` (every `AVA_TASK_MAINTENANCE_INTERVAL_SECONDS`, default 5 min, reminds owners of overdue in-progress tasks past their `remind_interval_seconds` window via a `chat` inbound; after `AVA_TASK_ESCALATE_N` (default 3) unanswered reminders, notifies the parent task's owner. Cluster-wide, runs once on the gateway. Discovered whenever the `ava_fleet` plugin code is present; gate its cluster-level on/off with `AVA_TASK_MAINTENANCE_ENABLED`) | `ava_builtins.plugins.ava_fleet.task_maintenance.healthcheck` (`/healthz` :8108) |
| `events-maintenance` (gateway only) | `.venv/bin/python -m services.events_maintenance.daemon` (every `AVA_EVENTS_MAINTENANCE_INTERVAL_SECONDS`, default 1h. Each pass (1) ensures the current + next UTC-month `agent_events` + `events` partitions exist — keeping partitions ahead of the write frontier so writes never strand in the DEFAULT catch-all — (2) applies the `events` retention policy (`services.events_maintenance.retention`): drops a month partition once every category in it has outlived its retention and prunes the expired categories (audit 365d / telemetry 90d / log 30d by default, tunable via `AVA_EVENTS_RETENTION_*_DAYS`; legacy `agent_events`/`event_log` are out of scope until the migration) — and (3) recomputes the Since-Birth day-grain rollups — `agent_metrics_daily` / `agent_model_tokens_daily` (the durable token+cost ledger) — from **Loki** (the unified event stream's live store): whole retained days up to yesterday (UTC), clamped to Loki's 168h retention floor (an outage longer than retention loses those days' aggregates — logged loudly; the JSONL mirror is the manual recovery source; pre-LGTM history was backfilled once by the llm-cost-rollup-columns migration from the frozen PG archive). Today is served live by the readers (whole-life cost = ledger + Loki tail from the watermark). Full-day overwrite upsert keyed on the PK ⇒ idempotent; partition creation and the retention pass are idempotent too (an already-covered month is a no-op; a dropped partition / pruned category is gone for good). Cluster-wide, runs once on the gateway — it owns the data plane. The rollup, checkpoint reaper and blob vacuum are unconditional; `AVA_EVENTS_MAINTENANCE_ENABLED` gates only the PG events-archive slices — partition rolling, retention, index governance) | `services.healthchecks.events_maintenance` (`/healthz` :8109) |
| `restarter` ★            | `.venv/bin/python -m services.restarter.daemon` — runs three per-tick controllers over this host's agent rows. **RespawnController** (`ops/controllers/respawn.py`): restart dispatch loop; also reaps this host's dead rows -> `terminated`: `status='starting'` rows whose pid is dead, `status='allocated'` rows stuck past `allocated_reap_grace_seconds` (a boot that dies between claim and the run loop, or before claiming at all), and `status IN ('running','idling')` rows whose pid is dead (a live agent that died silently — OOM/SIGKILL/crash — and would otherwise masquerade as alive forever, since the gateway's zombie-reap only fires lazily on interaction) — all would strand without it. The running/idling sweep never touches a live pid, so a normally-parked idle agent is never reaped; **none of the three reapers scan `hibernating`, so a swapped-out agent (with a dead prior pid) is never reaped.** **HibernateController** (`ops/controllers/hibernate.py`): memory swap-out/swap-in. Swap-out (gated on `AVA_HIBERNATE_ENABLED`) SIGUSR1s `idling` agents idle past `AVA_HIBERNATE_IDLE_THRESHOLD_SECONDS` (default 450s, deliberately above heartbeat's 300s so it reclaims mainly heartbeat-paused agents — a non-paused idle agent is woken by the heartbeat first) with no pending inbound, parking them `hibernating` (process freed) — except this host's `AVA_HIBERNATE_MIN_ACTIVE` most recently active agents (default 100, warm-pool floor ranked by `last_active_at` among `running`/`idling` only, so a `hibernating`/`terminated` row never occupies a slot), which stay resident regardless of idle time. Swap-in (always, even when disabled — else disabling would strand them) relaunches a `hibernating` agent the moment it has a pending inbound (a heartbeat check-in, a chat, a task), a clean restart with no marker. Hibernation is invisible to the SDK/frontend (both project it to `idling`). **CrashResurrectController** (`ops/controllers/resurrect.py`): brings back agents that died involuntarily (`terminated` with `termination_source IN ('reaper','launch-confirm')`) while a pending inbound waits — detailed in the git log (crash-auto-resurrect design). | `services.healthchecks.restarter` (`/healthz` :8102) |
| `milvus`                 | `.venv/bin/python -m services.milvus.daemon` (`milvus-lite server` gRPC :19530, data dir `~/.ava/milvus-data/`) | `services.healthchecks.milvus` (TCP probe :19530) |
| `memory-indexer`         | `.venv/bin/python -m services.memory_indexer.daemon` (watchdog fs watch `~/.ava/memory/` + Gemini Embedding 2 → milvus collection) | `services.healthchecks.memory_indexer` (`/healthz` :8105) |
| `frontend`               | `cd ui/web && NEXT_PUBLIC_GATEWAY_PORT=<AVA_GATEWAY_PORT> npm run build && npm run start` (Next.js prod build, 0.0.0.0:3000; the build-time port is injected from `AVA_GATEWAY_PORT` so the browser dials the gateway on the right port even when it is not the default 8000) | `services.healthchecks.frontend` (curl) |
| `gateway-watchdog` ★ (gateway only) | `.venv/bin/python -m services.watchdog.daemon --role gateway` (asyncio imports + runs the gateway-capability healthchecks above — redis-acl first (re-affirms the cluster's redis ACL user (the identifier its redis_url carries), which a redis-server restart silently drops), then pgbouncer (restarts the per-cluster pooler when its listener stops answering — when the pooler is enabled it is every consumer's AVA_DB_URL, so it comes before any service that would be revived without a database), then gateway/labeler/heartbeat/events-maintenance/task-maintenance/report/milvus/memory-indexer/frontend — every 60s) | the OS-scheduled **watchdog probe** (`ava cluster watchdog-probe --role gateway`, launchd / crontab / schtasks, every 60s) respawns it when its pidfile shows it dead |
| `agent-runner-watchdog` ★ (agent-runner only) | `.venv/bin/python -m services.watchdog.daemon --role agent-runner` (asyncio imports + runs the agent-runner-capability healthchecks above — ops/restarter (+browser, browser-mcp) — every 60s) | the OS-scheduled **watchdog probe** (`ava cluster watchdog-probe --role agent-runner`, launchd / crontab / schtasks, every 60s) respawns it when its pidfile shows it dead |
| `browser` (agent-runner only, auto-detect display; opt-out `AVA_BROWSER_ENABLED=false`) | `.venv/bin/python -m services.browser.daemon` (headed real Chrome, dedicated profile `~/.ava/chrome-profile/`, CDP :9222) | `services.healthchecks.browser` (HTTP probe `/json/version` :9222) |
| `otel-collector` | `<otel-collector-dir>/otelcol-contrib --config <otel-collector-dir>/config.yaml` (native Go binary installed by converge; one per machine — the local OTLP entry every agent exports to, fanning out to Tempo/Loki/Prometheus + the local JSONL trace mirror; file-backed queue absorbs backend outages) | `services.healthchecks.otel_collector` (OTLP POST probe on 4318) |
| `browser-mcp` (agent-runner only, gated with `browser`) | `.venv/bin/python -m services.browser.mcp_daemon` (one shared `chrome-devtools-mcp` upstream attached to the headed Chrome, multiplexed over a Unix socket `~/.ava/chrome-mcp.<cdp_port>.sock` to every agent's chrome bridge — serial, with per-connection page affinity so one Chrome client is shared instead of one per browser-using agent) | `services.healthchecks.browser_mcp` (Unix-socket `list_tools` probe) |
| `computer-mcp` (agent-runner only, platform-gated: signed permissions helper enabled + capable, AF_UNIX transport, non-Windows host — Windows is the phase-3 pilot) | `.venv/bin/python -m services.computer.mcp_daemon` (computer-use executor: every desktop action through the signed permissions helper — serialized machine-wide, screen-coordinated (lease + FIFO queue + `release_control`), Vision OCR on snapshots, audited as `computer_action` + `computer_session_start/end` events, served over `~/.ava/run/computer-mcp.sock`) | `services.healthchecks.computer_mcp` (Unix-socket lock-free `ping` probe) |
| `mcp-daemon` (agent-runner only) | `.venv/bin/python -m ava._mcps_daemon` (ONE shared MCP daemon per machine, serving every agent over `~/.ava/run/mcp_daemon.sock` — sessions isolated per client connection, replacing the old one-daemon-per-agent children) | `services.healthchecks.mcp_daemon` (Unix-socket `ping` probe) |

All sessions have cwd set to the prod path `~/.ava/source/` (see "Prod and dev clone paths" above).
Session commands run under `bash -lc` (#476) — the login-shell flag pulls in the user's
`~/.bash_profile` / `~/.profile` so `~/.local/bin` (where `uv` typically lives on WSL / Linux) is on
PATH without needing a sudo-installed symlink. macOS dev hosts already inherit login-shell PATH from
Terminal.app; the change is load-bearing only for Linux / WSL agent-runners.

Agent process sessions are named `ava-agent-{N}` (N == agent_id) — the record/session
name `ops/agent_launch.py:_launch_agent_process` keys by; all daemon services follow the same
`ava-<service>` convention (`cli/commands/_repo.py:session_name()`). Agent processes, daemon
services, and agent shells / watchers are native-style sessions, so none of them are shell
panes; enumerate live agents via the native-supervisor records
(`native_proc().list_sessions()`) or `agents_meta` — both surfaced in `ava status` /
cluster status (the `agent_count` field) — and agent shells via
`python -m shared.pty_sessions.cli list`. `ava cluster status` enumerates
every live session — services, orchestration (updater / rollout / cluster-restart),
agent processes and shells. Raw session stdout is queried in Loki (see
Logging / diagnostics below).

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
the agent boot chain only; it does not affect the `allocated→claim` window (the
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
  still absent/empty **and** a human is at the TTY, converge's `_ensure_browser`
  (`services/browser/profile.py:ensure_browser_profile`) offers to seed it by
  **copying your daily Chrome profile** (macOS `~/Library/Application Support/Google/Chrome`,
  Linux `~/.config/google-chrome`) into `~/.ava/chrome-profile/` instead. Copying
  hands the agent your full logged-in identity (cookies, sessions, saved
  passwords, signed-in accounts) so it acts as you without a re-login — a security
  trade-off, so it is opt-in behind an explicit confirmation and the default is a
  fresh profile. The copy excludes lock/socket files (`Singleton*`) and
  regenerable caches, reports its size first, and refuses while Chrome is still
  running (copying live SQLite risks a corrupt import — quit Chrome and retry).
  **Guardrails**: an already-populated profile is never touched (idempotent across
  restarts; prod's multi-GB logged-in profile survives every start); non-interactive
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
- **macOS headless limitation**: `display_available()` always returns True on
  macOS (no cheap way to distinguish a headless Mac from a headed one). A
  headless Mac with Chrome installed will pass `browser_capable()`, but
  Chrome may fail to open a window without a logged-in desktop — this is a
  pre-existing probe blind spot, not introduced by the default change.
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

Ava's dependencies are deliberately heavy — a full Postgres 17 + Redis 8.2, a
LangGraph checkpoint per agent, a Next.js frontend, and (macOS has no
fork-zygote) one real OS process per spawned agent. The framework does not
pretend otherwise; several independent layers instead keep that weight from
turning into a memory wall at fleet scale:

- **Hibernation (the flagship layer)** — an agent `idling` past
  `AVA_HIBERNATE_IDLE_THRESHOLD_SECONDS` (default 450s) with no pending inbound
  has its process killed outright and its row parked `hibernating`; a later
  inbound relaunches it (p50 919ms / p90 1.4s cold start). This is a genuine
  reclaim, not a suspend-to-disk: an idle agent's ~36MB resident heap + ~11MB
  MCP daemon + 2 pooled Postgres connections + Redis subscription all go to
  zero between wakes. Detail + the memory math:
  [`decisions/2026-07-20-agent-hibernation.md`](../decisions/2026-07-20-agent-hibernation.md);
  the controller (`HibernateController`) is the `restarter` service row above.
- **Heartbeat feeds hibernation, it doesn't fight it** — the heartbeat's own
  idle threshold (`AVA_HEARTBEAT_IDLE_THRESHOLD_SECONDS`, default 300s) is
  deliberately *below* hibernation's 450s, so a normally-idling agent is nudged
  awake before it is ever swap-out eligible; hibernation's dominant reclaim
  case is instead the agents that paused their own heartbeat
  (`ava.self.pause_heartbeat`), which get near-total reclaim for the whole
  pause window. [`decisions/2026-06-22-heartbeat-opt-out-over-escalation.md`](../decisions/2026-06-22-heartbeat-opt-out-over-escalation.md).
- **Warm-pool floor** — `AVA_HIBERNATE_MIN_ACTIVE` (default 100) exempts a
  host's N most-recently-active agents from swap-out no matter how long they
  idle, trading a little resident RAM for zero cold-start latency on the
  agents most likely to be reused next. It is a per-host knob (`scope=host`),
  sized to that box's own RAM budget, not a cluster-wide policy.
  [`decisions/2026-07-21-hibernate-warm-pool-floor.md`](../decisions/2026-07-21-hibernate-warm-pool-floor.md).
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
  size even before hibernation reclaims the agent entirely.
  [`agent/db.ava.okf.md`](../agent/db.ava.okf.md).

None of this claims memory stops mattering — it is the honest current floor.
The hibernation decision's own consequences section already redirects
attention to the *next* walls once memory is handled: the heartbeat's
wake-rate ceiling (~1.67/s, ≈750 agents on today's numbers) and LLM turn cost,
which is linear in fleet size regardless of any of the above.

### Start / check / restart

Day-to-day ops **only uses the `ava` CLI** — one line to bring up the full set / check status / stop everything:

```bash
ava start [--machine-name X --serve-gateway --serve-agent-runner --memory-remote URL --gateway-url URL]
             # THE bring-up — the only one. On first run for a cluster it BIRTHS it (allocates ports/db, brings up the cluster's own pg/redis instance, provisions the db, writes the cluster .env), then brings up the union of the host's capabilities' services: gateway capability -> this cluster's own native pg/redis (pg_ctl + redis-server under $AVA_HOME) + its service sessions; agent-runner capability -> 3 service sessions (ops/restarter/agent-runner-watchdog); a single-box host runs both (and BOTH per-capability watchdogs). Each capability is its own --serve-* flag.
             # Pure bring-up: the home comes from the checkout-anchored boot (never the cwd, never a flag — identity is the path). An uninstalled home fails fast pointing at install.sh / install.sh --worktree / ava enroll by role. First run on a fresh install still takes --machine-name (+ --gateway-url if the install didn't write it); serve flags come from the install's --role via .env. idempotent
ava status   # one screen of sessions / probes (http / tcp / pid); filtered by role (agent-runner skips the pg/redis probe). A gateway host also gets a `gate (fleet UI entry)` section: whether anything answers on the entry port (:3000) and whether this cluster's own supervisor still holds the gate (launchd label / pidfile). ANY HTTP status counts as answering — the gate serves 503 (updating) and 502 (app rebuilding) by design, so a 2xx test would call a correctly-working gate dead mid-rollout; it is liveness-only for the same reason the frontend row is (the entry port is the public bookmark and carries no identity payload), which is what the supervisor line covers. Both halves also ride in `ava cluster health-probe`'s check 5 — alert-only, never feeding auto-rollback, since a gate that failed to register is not a code regression and no rollback puts it back. On any installed host warns if the prod source `$AVA_HOME/source` has drifted off `main` (an agent that developed in the prod tree instead of a worktree) — it runs the live cluster, so a feature branch there means un-reviewed code live + force-discarded on the next rollout. Also shows the cluster pin (`cluster_target_sha`, the commit the last rollout pinned the cluster to) vs this host's HEAD, so a node that missed a rollout is visible (read-only; fail-fast-on-drift is future — see commit-pinned-cluster.md)
ava stop     # kill of the service sessions via the session backend (graceful SIGTERM to the top process, tree SIGKILL on timeout) + reap this cluster's agent sessions (ava-agent-<id>: SIGTERM so each agent tears down its MCP daemon / Claude Code subprocess, then force-kill stragglers — they are spawned via the gateway, not ServiceSpecs, so nothing else stops them; agent shells/watchers (per-session pty hosts) are force-killed) + stop this cluster's own native pg/redis (gateway: pg_ctl stop + redis shutdown, data persists on disk; agent-runner skips); stdin y to confirm. First best-effort POSTs /api/cluster/stopping?machine=<self> so the cluster view shows this host as "stopped" (intentional) not "offline" (crash) — the live probe can't tell them apart. Stays local by design: it kills the gateway + its own data plane, which can't be delegated to the gateway
# Read AVA_CLUSTER_SECRET without echo and export it for this command first; unset it afterward.
ava enroll --gateway URL --machine-name NAME --machine-host HOST   # join a split-deployment agent-runner to a gateway (presents AVA_CLUSTER_SECRET to the gateway's authenticated /api/bootstrap without putting it in argv); --machine-host is this runner's reachable address (written to the $AVA_HOME/machine_host file, so a re-enroll keeps it) — the gateway dials its ops server there; verifies the runner projection, which every runner process re-fetches at startup, then run `ava start`. Add --health-port-base N (a block base on the allocator's grid, 18000 + k*16, e.g. 18112; pick one no local cluster already owns — `ava cluster ls`) only when another Ava unit shares this machine's localhost namespace — daemon health ports are a per-UNIT fact the gateway does not serve, and two units otherwise take the same defaults 8102-8109. A WSL2 host auto-applies its own reserved base when the flag is omitted (issue #1152), since a co-located native Windows unit would otherwise default to that same shared block; a base already in `.env` — hand-set or auto — survives a bare re-enroll
ava cluster update   # a gateway-capable host (incl. single-box) spawns the three-phase orchestration in a detached `ava-rollout` login-shell session (runs `ava cluster update --local`) and returns — directly via `gateway.cluster.spawn_rollout`, NOT by POSTing its own gateway (the rollout must outlive the request, so it runs detached; the `/api/cluster/rollout` endpoint stays for the frontend Update button). `--local` forces the in-process orchestration (what that detached session runs; also for debugging). Every in-process leg (`--local`, `--restart-only`, the agent-runner self-update, and `ava restart`) refuses to run from inside a supervised session — a pty-hosted agent shell, an agent process, a service daemon — because its own stop leg kills that session's whole tree, the orchestration with it (the 2026-08-12 stranding; `shared.proc.hosting_supervised_session`; the detached orchestration sessions are exempt). A pure agent-runner `ava cluster update` is a light in-process self-update. See "Multi-machine ava cluster update orchestration" below
ava cluster status   # full multi-machine roster (thin client: GET /api/cluster/roster; gateway resolves its own row + probes each agent-runner by dialing a status_probe op to its ops server). online = live probe answered; stopped = machines.stopped_at set by `ava stop` (cleared by `register_self()` — on `ava start`, and on the ops daemon's own boot); offline = neither (crash / unreachable); STALE-STOP = the probe answered AND the stop marker is set, i.e. the two sources of truth disagree — the marker is the wrong one (a latch, cleared only by a `register_self()`), and until it is cleared the `ava cluster update` fan-out, which filters on that same marker, would drop this host; the next rollout reconciles it (`_resolve_fanout_targets`). The `up since` column is that same `register_self()` stamp (`machines.up_since_at`) — a boot/announce time, never a heartbeat: nothing refreshes it while a host merely keeps running, which is why `online` is the live probe and not a freshness test on this column. The `pin` column shows each node's HEAD vs the cluster pin (`cluster_target_sha`): ✓ on-pin / ✗ off-pin (missed a rollout) / ? no pin yet or HEAD unknown — the gateway computes the verdict (`on_pin`) server-side so the roster stays a bare list. Same per-node pin view on the Control page; the `agent-runner-watchdog` tick also self-heals when this host is off-pin — `_check_pin_drift` force-updates it to the pin via a spawned update (`_spawn_update(target_sha=pin)`, cooldown-guarded, skips that tick's healthchecks; a paused host returns at the `_tick` gate first so it never fights a rollout). It also declines while ANY update is in flight on this host, not only while the cluster lease is held — being off-pin is a running update's own mid-flight state, and a watchdog-spawned `ava-updater` takes no lease, so the lease-only check let this force the checkout back underneath a live updater and flap prod between two commits for two hours ([decision](../decisions/2026-07-31-two-healers-must-not-own-the-same-checkout.md), issue #1074). If you see this dimension idle on a host that is off-pin, look for an `ava-updater` session before assuming the heal is broken. The `gateway-watchdog` only warns on an off-pin gateway (`_warn_gateway_off_pin`) — a gateway drift needs a rollout, not a single-host self-checkout. The `code` column is the separate question the pin cannot answer: it shows the commit the process that answered the probe froze at ITS OWN boot (`shared/process_sha.py`), so `⚠` there means the checkout moved but that process was never restarted — a node can read `pin ✓` and `code ⚠` at once. `ava start` will not clear it (it skips already-running sessions); `ava restart` will — and on an agent-runner the `agent-runner-watchdog`'s **code controller** (`ops/controllers/code.py`) now spawns exactly that restart on its own once HEAD is on the pin but the running commit is not. It is the second half of the pin dimension: `_check_pin_drift` heals *off*-pin hosts, so an on-pin host running old code (a Phase-B checkout whose restart declined — the 2026-07-28 wsl state) used to be off every controller's map and needed a human. Same guard set as the pin heal (agent-runner only, declines while the cluster update lock is held or a local orchestration session is alive, persistent per-commit backoff, shared process cooldown), and it never runs outside the prod source tree — with one exception, which is why a `pin ✗` or `code ⚠` host beside a `waited-on` cell now clears in a watchdog round instead of on the hold's TTL: a **settle hold naming this host** is not a deploy mutating the cluster, it is a stated waiting period whose content is "this host has not converged", and nothing executes under it, so deferring to it made the hold and the heal wait on each other (`DeployLease.awaits`, issue #1020). The pin heal takes the same exception for the same reason — `settle_hosts_converged` will not release until the named host reports BOTH `head_sha == pin` (pin's dimension) and `running_sha == pin` (code's). A lease with no note — a rollout executing right now — still defers, as does a hold naming someone else. The stranded-pause recovery takes that same exception (issue #1116) rather than deferring to any live lease: unpausing is not itself convergence, but a paused host cannot reach the heals that are, because the pause gate blocks the whole round ahead of them — so a host that is both paused and waited-on was the one host forbidden to do the thing the hold was waiting for. Per-daemon granularity is on each daemon's own `/healthz` `sha`. The `hold` column is the third dimension, and the only one that is not a probe: it is transcribed from the live `cluster_update_lock` lease (`deploy_hold` = the lease sentence, stamped cluster-globally onto every row and printed once as a banner above the table; `waited-on` = this host is named in a settle hold's note). It answers "why was my deploy refused", which until now was legible only in the health-probe cron log or by reading the lock row by hand — but it answers ONLY that: `waited-on` is the hold's recorded set, not a live convergence verdict (`pin` / `code` are), a blank cell is "not named by the hold" and not "converged" (a host that never acked is never named), and a blank column is not proof no deploy is running, because a watchdog-spawned host-local `ava-updater` takes no lease at all. The roster deliberately reads the lease row rather than calling `deploy_in_flight()`, which would probe every machine and *release* a converged hold — neither belongs on a status GET. Above both banners sits the **last-update banner**: a rollout writes its own outcome to `cluster_last_update` (`shared/last_update.py`) — opened before Phase A and closed in the orchestration's `finally` — and the roster states it when it FAILED, because the `pin` and `code` columns alone are symptoms several unrelated states share (a node that missed a rollout, a checkout moved without a restart, a rollout that failed and rolled back all read the same there). **Pin semantics on failure:** a failed rollout does NOT roll the pin back. If the gateway reached its target before failing, the pin advanced and the hosts still off it converge via their own watchdog; if it failed earlier, the pin was never moved and nothing is converging toward the failed target — the banner says which. A rollout whose orchestration is killed never closes its row, and that is read as `orphaned` once its deploy lease lapses (the record is written ahead of the work precisely because the dying process cannot file it). A successful `ava cluster update` replaces the record, so nothing has to remember to clear a failure. The banner also carries the **rollback anchor** (`cluster_pin.last_known_good_sha`, surfaced beside the pin on both the roster and the status page): it was recorded since the pin existed and shown nowhere, so a rollback used to present as the pin simply moving to an older commit. And when an external observer has acted on the failure — today `ava cluster rollback`, which the health probe's `--auto-rollback` shells into — it writes what it did onto the record (`observed_by`, e.g. `rolled back 8bdd366 -> 7e571b4`) without touching the verdict: the dying orchestration files nothing, but the process cleaning up after it witnessed the death, and that sentence is what makes the surfaced failure actionable rather than merely visible. A failure the cluster has already come back from reads `recovered` rather than `aborted`, which is a different call to action — nothing to repair, only something to know, so both surfaces mark it as a warning instead of a failure. It is reached from either side: the orchestration writes it when its own gateway leg rolled back to last-known-good (rc=1 on the pull path), and a reader derives it for any failed record carrying an `observed_by`, which is how a rollback that cleaned up after a dead orchestration closes the 2026-07-30 story. Finally, the record names the rollout's own log (`log_path`), threaded down as `ava cluster update --rollout-log` by the `spawn_rollout` that created the file — so the banner points at THE log rather than at the `$AVA_HOME/logs/rollout-<epoch>.log` glob. It is stamped once, by the intent write, and no later writer touches it; a foreground `ava cluster update --local` is not teed to a file and records nothing, since naming an older rollout's log would be worse than naming none
ava cluster restart  # bounce the WHOLE cluster (this host + fan out to agent-runners, no git pull) via POST /api/cluster/restart; `ava restart` is the local single-host form
ava cluster ls       # list all registered clusters in ~/.ava/clusters.json
ava cluster down --path PATH   # stop the cluster at a home path (its gateway + its own pg/redis instance), keeping its registry slot + data dirs (the safe way to stop a dev worktree cluster from another checkout)
ava cluster destroy --path PATH [--drop-db]   # stop a cluster + free its registry slot (port block) + deregister its OS-scheduled jobs (health probe, both watchdog probes, autostart); --drop-db also removes its pg/redis data dirs; refused for the default home ~/.ava
```

**`ava start`'s exit code means something.** Three outcomes, because "the start
sequence ran" and "this host is serving" are different facts:

| rc | Meaning | What to do |
|---|---|---|
| `0` | every step ran and every launched service passes its liveness probe | nothing |
| `4` | every step ran; a launched service never passed its probe inside `SERVICE_READY_TIMEOUT_S` (180 s) | read the snapshot printed just above — the failing rows are named, with the session list repeated after the snapshot. The host is **up but incomplete**: the watchdog keepalive is already retrying, `ava status` re-checks, and `ava start` is idempotent to run again |
| `1` | a start *step* failed (converge, the data plane, migrations, the schema assertion, machine registration) | the host may have no services at all; the failing step printed why |

The gated set is derived, not listed: it is `ops/spec.py`'s roster for this host's
capabilities, minus anything `_gate_reason` skips (`browser` with no display,
`browser-mcp` with no AF_UNIX, a disabled `heartbeat`), minus
`--disable-service`, minus the frontend (whose ~30-60 s build would otherwise set
the floor for every gateway start). **A service that is skipped is not a service
that is unready** — it never reaches the gate and cannot fail a start. A service
with no probe at all (`browser-mcp`, whose transport is a Unix socket only its
healthcheck dials) likewise cannot: absence of evidence is not failure.

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
- **one-shot pipeline** — git pull + uv sync + apply pending migrations all run automatically; miss one step and
  you hit SchemaVersionMismatch (a prod upgrade stepped on this 2026-05-13). Manual 4-step equivalent = `ava cluster update` one line.
- **No stdin confirmation** — runs through automatically, no prompt blocking. `ava stop` still asks for confirmation (ops needs to be explicit
  about "I want it stopped now" before taking the hard-kill path).


## Private-network deployment (phone / multi-device access)

Both the gateway and frontend bind all interfaces on the **gateway host**
(the gateway both address families, the frontend `-H ::`); any
private-network device (laptop, phone, other agent-runners) hits them directly at the
gateway's private-network address — gateway on `:8000`, frontend on
`:3000`. The exact host is whichever node holds the gateway role (a single-box
deployment's only host). The access model below is the authoritative
description of ports and trust boundary.

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
and [`Cluster secret rotation`](#cluster-secret-rotation) below for the secret
itself. The user opens the UI / API at the gateway's private-network
address (on this deployment's tailnet, prefer the MagicDNS name over a raw
`100.x` IP — IPv6-only carrier networks NAT64-synthesize IPv4 literals and the
request never enters the tunnel):

- `http://<gateway-host>:8000` — gateway (API + SSE)
- `http://<gateway-host>:3000` — frontend

The frontend resolves the gateway as `${location.hostname}:8000` (frontend
`:3000` and gateway `:8000` are co-located on the gateway host, different
ports). Gateway CORS allows all origins — anything that reaches it is already on
the private network. On a host where another service holds the gateway port, set
`AVA_GATEWAY_PORT` plus the matching
`AVA_GATEWAY_HEALTH_URL=http://localhost:<port>/api/agents`
(two-var contract; `ava status` probes the health URL).

## Cluster secret rotation

`AVA_CLUSTER_SECRET` is the single per-cluster pre-shared key: the Postgres
role's password, redis's `requirepass` + the cluster's redis ACL user's
password, and (when PgBouncer is enabled) its client-facing scram credential —
and it is the bearer token every authenticated cross-machine call presents
(`/api/bootstrap`, `/ops`, `gateway_auth_headers()`; see the access-model note
above). A leak of it (e.g. via a captured `ps` output, an incident transcript)
means rotating all of that, end to end.

`scripts/rotate_cluster_secret.py` does the cluster-secret half. Run it on the
gateway box, against its own cluster (checkout-anchored `settings`, like any
other `cli`/`scripts` entry point — there is no `--home` flag):

```bash
.venv/bin/python scripts/rotate_cluster_secret.py            # dry-run (default): read-only,
                                                               # prints the plan + preflight probes
.venv/bin/python scripts/rotate_cluster_secret.py --execute   # mints a new secret and rotates
```

It reuses the exact idempotent "ensure" primitives `ava start` already calls
on every bring-up (`shared.cluster.ensure_cluster_role`,
`shared.cluster.ensure_cluster_redis_acl`, `cli.commands._pgbouncer.ensure_pgbouncer`)
— rotation is just calling them with a new secret instead of the current one.
Sequence: mint -> re-affirm the Postgres role's password (loopback trust
socket, no restart) -> re-affirm the redis ACL user + flip `default`'s
`requirepass` (both live `CONFIG SET`, no redis-server restart — this
cluster's redis runs `--save ""`, so a restart is an avoidable data-loss
event) -> reload PgBouncer's `userlist.txt` (SIGHUP, when enabled) -> verify
the new secret authenticates everywhere and the old one no longer does ->
rewrite this gateway's own `.env` (`upsert_env`, which snapshots the old
`.env` first). Any failure writes a JSON recovery state
(`$AVA_HOME/backups/secret-rotation/rotate-<timestamp>.json`, `0600` — holds
both secrets in plaintext, same posture as `.env` itself) recording the last
completed phase and prints the exact `--resume` command; every phase is safe
to re-run.

**What it deliberately does NOT do** (left to the operator — narrow blast
radius, no new service-lifecycle coupling in the script):

- **Restart the gateway process.** Existing connections survive the pg/redis
  steps untouched (neither kicks a session on a password change); the
  gateway's in-memory Settings only pick up the new secret on its next boot.
  Bounce it once the script reports success.
- **Push the new secret to already-enrolled agent-runners.** A runner fetches
  its cluster config (db/redis URLs, channels, provider keys) from the gateway
  at every process start, but its own `AVA_CLUSTER_SECRET` — the bearer for
  that very fetch — cannot be re-pulled through the endpoint it gates. The
  script prints the current agent-runner roster (name + URL, read from the
  `machines` table) as a checklist — for each: hand-edit `AVA_CLUSTER_SECRET`
  in that host's own `.env` (or, once the gateway expects the new value, expose
  it to the command as `AVA_CLUSTER_SECRET` and re-run `ava enroll --gateway ...
  --machine-name ... --machine-host ...`), then restart it. Do not put the
  secret in argv or shell history.
- **Provider API keys.** They live in the same `.env` but rotate through each
  provider's own console — checklist below.

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
| `XAI_API_KEY` | console.x.ai -> API keys |
| `GLM_API_KEY` | open.bigmodel.cn -> API keys |
| `MOONSHOT_API_KEY` | platform.moonshot.cn -> API keys |
| `MIMO_API_KEY` | this provider's own developer console |
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
**OTel + Tempo + Loki + Prometheus + Grafana**. Every machine runs one **OTel
Collector sidecar** (`ava-otel-collector` session, supervised by the
watchdog, binary + config installed by converge from
`deploy/otel-collector/`); agents export OTLP/HTTP to their LOCAL sidecar
(`AVA_TELEMETRY_OTLP_ENDPOINT`, default `http://127.0.0.1:4318`) and never
dial a backend directly. The sidecar fans out:

- **traces** → Tempo OTLP/HTTP (`AVA_TELEMETRY_TEMPO_ENDPOINT`, default
  `http://127.0.0.1:14318` on the LGTM host) + local JSONL mirror
  (`$AVA_HOME/traces/spans.jsonl`, rotated `spans-<ISO>.jsonl`).
- **logs** — every unified event (the `events` table write path) dual-writes to
  OTLP logs (Loki) via `shared/telemetry_otlp.py` → sidecar → Loki
  (`AVA_TELEMETRY_LOKI_URL` base, `/otlp` appended).
- **metrics** — telemetry events' numeric payloads map to OTLP metrics
  (Prometheus): int -> counter, float -> histogram, named `ava_<event>_<field>`;
  sidecar → Prometheus OTLP receiver (`AVA_TELEMETRY_PROMETHEUS_URL` base,
  `/api/v1/otlp` appended).

The whole OTLP surface (exporter + trace recording + ship) is gated by
`AVA_TELEMETRY_OTLP_ENABLED` (default **on**); off = Postgres-only writes, one
kill switch. Applies on the next process start.

**LGTM backend lifecycle** — the Tempo/Loki/Prometheus/Grafana compose stack
(`deploy/lgtm/`) is the cluster's observability backend, required while the
gateway serves /ops and the inspect endpoints (consumers: the gateway
Loki/Prometheus read paths, ops alerting via Grafana's embedded Alertmanager
→ the gateway webhook, the events-maintenance Loki rollup, `ava cluster
health`). It is a **host singleton** owned by the lifecycle on exactly one
home — the one carrying the operator-created `$AVA_HOME/lgtm-host` marker
file (in practice prod `~/.ava`; `touch ~/.ava/lgtm-host` once). On that
host, converge runs the idempotent `deploy/lgtm/start.sh` on every `ava
start` / `ava cluster update`, the gateway watchdog re-runs it when the
readiness probes (Loki/Prometheus/Tempo/Grafana) hit connection failures
(`services/healthchecks/lgtm.py`), and `ava status` shows the containers +
probes. Unmarked homes (dev worktree clusters) never touch the containers.
Deliberate stop: remove the marker or `ava start --disable-service lgtm`,
then `deploy/lgtm/stop.sh` — see `deploy/lgtm/README.md`.

**Recording is one local hop** (sidecar architecture, task #1266). The
previous inline-POST design raised `Exception while exporting Span.` whenever
the POST failed; the agent-side mirror (record/ship split 2026-06-16) fixed
that but left the mirror as the only durable copy. Now recording is an OTLP
export to the local sidecar, whose **file-backed sending queue** (file_storage
extension) absorbs backend outages — a dead Tempo/Loki/Prometheus never drops
what the sidecar accepted, and the queue survives sidecar restarts. The
events/metrics exporter follows the same rule: `shared/telemetry_otlp.py`
sheds (counted) instead of blocking, so an unreachable sidecar never touches
the Postgres write (see its docstring for the full isolation contract).

**Record** — `shared/trace.py:initialize_tracing`, gated by `AVA_TRACE_ENABLED`
(default **on**). Instrumentation is OpenLLMetry (`traceloop-sdk`); the sole span
exporter is `OtlpJsonHttpSpanExporter`, which POSTs each export batch as one
standard OTLP/JSON `ExportTraceServiceRequest` to the local sidecar's
`/v1/traces` (JSON wire format; LLM content stripped before it leaves the
process). The sidecar's file exporter mirrors each batch line-for-line to
`$AVA_HOME/traces/spans.jsonl` — the durable, vendor-neutral, grep-able
source of truth (any OTLP backend ingests the same lines; rotation bounds the
directory by size/day/backups, and the agent-start prune enforces
`AVA_TRACE_RETENTION_DAYS` / `AVA_TRACE_MAX_DIR_MB` as the final guard). A
sidecar not answering at agent init disables recording for that process
(reported) — the same init-time tradeoff the events exporter makes.

**Ship** — `ava trace ship` (`cli/commands/trace.py`). Recovery replay: reads
the mirror and POSTs each line as OTLP/HTTP protobuf straight to
`{AVA_TELEMETRY_TEMPO_ENDPOINT}/v1/traces` (Tempo, no auth) — bypassing the
sidecar, because replaying through it would write the replayed lines back
into the mirror (watermark loop). Needed only for gaps the queue could not
hold (backend down longer than the queue, offline machines, past windows).
Gated by `AVA_TELEMETRY_OTLP_ENABLED` (refuses while off — one kill switch
for the whole OTLP surface). The old 5-minute ship schedule (gateway
schedule id=5) is obsolete for liveness — the sidecar fans out live; keep it
only if you want scheduled gap-replay.

- **incremental** (no args): a per-file byte-offset watermark
  (`traces/.ship-watermark.json`) advances per POSTed line, so re-running ships
  only new lines and an interrupted ship resumes exactly where it stopped.
- **windowed** (`--since` / `--until`, `YYYY-MM-DD`): ships matching files whole,
  ignoring the watermark — the "shipping was off, import a past range" path. Span
  ingestion is idempotent by span id, so re-shipping is safe.

The toggle gates *shipping*, not *recording*: a window recorded while
`AVA_TELEMETRY_OTLP_ENABLED=false` is still on disk and ships later via the
windowed mode. (Bench containers record to their ephemeral-FS mirror, which
dies with the container — a host that wants bench traces in Tempo ships its
own mirror after the run.)

**Explicit instruments + per-agent session_span**: `Traceloop.init` is called
with `instruments={ANTHROPIC, OPENAI, LANGCHAIN, GOOGLE_GENERATIVEAI}` —
LangGraph nests through the LANGCHAIN instrumentor (its callback handler), so
there is no separate LANGGRAPH instrument. Around `graph.ainvoke`,
`agent/loop.py` opens `session_span(name="ava-agent-N",
session_id=str(agent_id))`, a native OTel root span stamped with the neutral
`session.id` (the viewer groups one agent's spans into a session by it). All
child spans (LLM calls, tool execs, retries) share that root's trace_id +
parent; without the wrap each LLM call is an orphan.

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
| raw session stdout (gateway / agents / shells / schedules) | Loki (the LGTM backend): promtail tails every `$AVA_HOME/logs/*.out.log` plus the updater/rollout tees — label `service` = session name, 7-day retention; see `deploy/lgtm/README.md` |

Raw session output is queried in Loki, not tailed from a file — Grafana Explore
(Loki datasource), `logcli`, or the HTTP API:

```bash
logcli --addr http://127.0.0.1:3100 query '{service="ava-gateway"}' --since=1h --limit=100
logcli --addr http://127.0.0.1:3100 query '{service=~"ava-agent-.+"}' --tail
curl -G -s http://127.0.0.1:3100/loki/api/v1/query \
  --data-urlencode 'query={job="ava-sessions"} |= "error"' \
  --data-urlencode 'limit=50'
```

Two label namespaces: raw session logs use `service`, the OTLP event stream uses
`service_name`. Agent loguru JSONL (`agent-{N}.log`) is not scraped — it already
reaches Loki structured via OTLP.

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
