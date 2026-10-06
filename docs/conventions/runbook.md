# Runtime model

## Clusters, units, prod, and dev clone paths

Current topology and authority have component owners:

| Topic | Owner |
|---|---|
| Home identity, capabilities, init, ports and admitted checkout | [Start identity](../../cli/docs/start_identity.ava.okf.md) |
| Owned or remote-managed PostgreSQL, Redis and PgBouncer startup | [Data-plane startup](../../cli/commands/data_plane/docs/data-plane-startup.ava.okf.md) |
| Native PostgreSQL custody and admin authority | [PostgreSQL owner](../../base/cluster/docs/postgres.ava.okf.md) |
| Runtime DB/API delivery and remote runner bootstrap | [Authority wiring](../../base/cluster/authority/docs/wiring.ava.okf.md) |
| Sealed unit bundle exposure and compromise boundary | [Unit bundle](../../base/cluster/authority/docs/unit-bundle.ava.okf.md) |
| Agent checkpoint persistence and crash replay | [Agent startup](../../agent/startup/docs/startup.ava.okf.md) |

Initialize the home before starting it. For a split cluster, start the gateway
before joining or starting runners; their Settings construction requires its
bootstrap endpoint. Use the [deployment capability](../../.agents/skills/ava-guide/deploy/SKILL.md)
for the installation sequence. Ordinary start refuses missing or contradictory
identity rather than inferring it from an existing listener or directory.

Checkpoint schema admission belongs to [agent infrastructure](../../agent/docs/infra.ava.okf.md).
For an upstream checkpoint dependency change, follow the
[checkpoint migration rule](../../db/docs/migrations.md#checkpoint-dependency-upgrades).
The interval's replay contract belongs to the agent startup owner above; its
operator verification and rollback protocol remains
[checkpoint interval canary](checkpoint-interval-canary.md).

Remote runners receive class DB/API credentials through a sealed bundle, not
the gateway's human secret. Guard the bundle and transport key as credentials;
use the unit-bundle owner to identify everything a compromise exposes, then
follow [manual rotation after a credential leak](#manual-rotation-after-a-credential-leak).
A generation is not automatically replaced on compromise; do not infer a
rotation procedure from an older design.

Application services belong to one root per home, while persistent PTYs and
native data-plane resources have separate custody; see
[root-owned lifecycle](#root-owned-application-lifecycle).
CI provisions a separate isolated native test surface; neither tests nor build
tooling may target a production home. See [CI](#ci-continuous-integration).

prod runtime and dev workspace are split at the filesystem level:

| Path | Role | Notes |
|---|---|---|
| `$AVA_HOME/source/` (default `~/.ava/source/`) | **prod** — cwd of the long-running service sessions | git working tree; upgrades go through `python -m cli.fleet_update` ([Updating a networked cluster in source mode](#updating-a-networked-cluster-in-source-mode); `ava.self.update()` was removed 2026-08) |
| `<development-checkout>/` | **dev clone** — root of worktree-driven development; dev worktrees live under `.worktrees/<task>/` (made by `scripts/setup-worktree.sh <task>`) or `.claude/worktrees/<task>/` (Claude Code's native worktree tool; complete it with `scripts/setup-worktree.sh` inside it) | freely checkout any branch, decoupled from prod |

### Worktree uv iron rule (Tasks #1572, #5638)

An editable install targets a virtualenv, not the shell's current directory.
Use the checkout's own real `.venv`, clear inherited `VIRTUAL_ENV`, and run
`guard_editable_venv.py` before dependency operations as described in
[development setup](dev-setup.md#development-in-a-worktree) and
[testing environment](testing.md#environment). The
[editable-install guard](../../cli/commands/docs/editable-install-guard.ava.okf.md)
owns exact-root validation and explicit repair; ordinary startup does not
permit changing another checkout's installation.

Before removing a worktree, establish that it has no live users and that no
needed stable virtualenv points at its source. Follow the
[contributor cleanup boundary](../contributing.md#submit-the-pr);
preserve the checkout if ownership cannot be established. The escaped defect
is recorded in [the editable-pointer incident](../postmortems/0006-an-editable-install-is-a-cross-checkout-pointer.md).

### Manual editable-install recovery

Use the [editable-install guard](../../cli/commands/docs/editable-install-guard.ava.okf.md)
from the affected stable checkout, then recheck both editable records before
removing a worktree. [Development setup](dev-setup.md#development-in-a-worktree)
owns environment isolation and dependency commands.

Runtime facts needed to choose the checkout and operation have component owners:

| Question | Current owner |
|---|---|
| Which home and initialized identity a CLI may operate | [Start identity](../../cli/docs/start_identity.ava.okf.md) |
| Which checkout supplies the CLI and Python installation | [Python installation](../../cli/docs/python-install.ava.okf.md) |
| PostgreSQL, PgBouncer and Redis startup/custody | [Data-plane startup](../../cli/commands/data_plane/docs/data-plane-startup.ava.okf.md) |
| Database roles and credential delivery | [Authority](../../base/cluster/authority/docs/authority.ava.okf.md), [wiring](../../base/cluster/authority/docs/wiring.ava.okf.md), [unit bundle exposure](../../base/cluster/authority/docs/unit-bundle.ava.okf.md) |
| Source-start host integration | [Host converge](../../cli/commands/converge/docs/converge-host-wiring.ava.okf.md) |
| External agent skill ownership and update | [Package commands](../../cli/commands/extensions/docs/packages.ava.okf.md) |
| Schedule source, provisioning and stored-script verification | [Schedules](../../schedules/README.md) |

Use the [deployment capability](../../ava_builtins/skills/platform/ava-guide/deploy/SKILL.md)
for a new unit and the [operations capability](../../ava_builtins/skills/platform/ava-guide/operations/SKILL.md)
for an existing cluster. Development preparation and a repository merge do not
authorize a runtime rollout.

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

Service registration belongs to [the service domain](../../services/docs/services.ava.okf.md);
native custody and generation-bound readiness belong to
[Ava Root](../../services/supervision/ava_root/docs/ava_root.ava.okf.md).
Persistent interactive shells use the
[PTY generation boundary](../../base/sessions/pty/docs/generation-boundary.ava.okf.md).
When stopping an older release, verify the actual native inventory as well as
the desired roster; configuration alone is not evidence of termination.

See [the checked service roster](../../ops/docs/service-roster.md) for the
registered services and their probes.


The gate preserves the browser `Host` while proxying to the loopback frontend,
so the frontend CSP derives the same host that its API client uses. A TLS or
reverse proxy before the gate must overwrite `X-Forwarded-Host` and
`X-Forwarded-Proto` with the public browser origin; the gate relays those
headers only when present. Use lowercase `http` or `https` for
`X-Forwarded-Proto`; the frontend normalizes other casing before deriving its
CSP origin. The gate also relays the frontend CSP and static browser-security
headers to the public response.

#### Optional HTTPS browser entry (HTTP/2)

See [Gate entry operations](../../services/entrypoints/gate/docs/operations.md)
for the optional private HTTPS/HTTP2 entry, rollback and browser acceptance.

### Backup and recovery posture

The recovery points are the daily encrypted logical dumps (`pg-backup`, due at
`AVA_BACKUP_HOUR` cluster time, the newest `AVA_BACKUP_KEEP` kept in
`$AVA_HOME/backups/db/`, published off-site under `ava-logical/` when
`AVA_BACKUP_OFFSITE_ENDPOINT`, `AVA_BACKUP_OFFSITE_BUCKET` and
`AVA_BACKUP_OFFSITE_CREDENTIALS_FILE` are set through `ava config set`), proved
by the weekly isolated logical restore drill. The self-written PITR stack was
deleted (`docs/decisions/2026-10-02-delete-the-self-written-pitr-stack.md`). Point-in-time
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
(`.agents/skills/ava-guide/operations/references/db-restore.md`) or copy it
into `backups/db/` (0600); the next scheduled run dumps again.

Application service commands, admitted PATH and process ancestry belong to
[start identity](../../cli/docs/start_identity.ava.okf.md) and
[root supervision](../../services/supervision/ava_root/docs/ava_root.ava.okf.md).
Enumerate application services through `ava status`; persistent shells through
`ava sessions list`. Idle and paused agent identities remain durable even when
no active turn task is running; see [agent runtime](../../agent/docs/agent-runtime.ava.okf.md).

PTY closure is best effort: known shells/terminals close with bounded signals;
background or detached processes can remain. Known job leftovers are diagnostic.
Inspect residual processes and OS stalls before explicit operational action;
there is no automatic host-wide kill or reboot. See the
[closure decision](../decisions/2026-10-07-pty-best-effort-closure.md).

### Emergency PTY allocation freeze

See [PTY allocation operations](../../base/sessions/pty/docs/operations.md) for
freeze, resume and marker repair, including the schedule-generation effect.

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

The [Codex ownership reference](../../ava_builtins/skills/platform/ava-guide/external-agents/references/canonical_codex_owner.md)
owns generation records, status and exact-generation cancellation. The
[launcher guide](../../ava_builtins/skills/platform/ava-guide/external-agents/references/codex.md)
and [resume reference](../../ava_builtins/skills/platform/ava-guide/external-agents/references/resume_after_interruption.md)
own commands and conversation recovery. Inspect the printed generation before
cancelling; a shared workspace can contain several live generations.

### Shared browser (`browser` service)

See [component operations](../../services/desktop/browser/docs/operations.md).

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
  [`docs/decisions/2026-06-22-heartbeat-opt-out-over-escalation.md`](../decisions/2026-06-22-heartbeat-opt-out-over-escalation.md).
- **Per-cluster data plane is sized to be noise, not a multiplier** — every
  cluster (including each dev worktree) runs its own Postgres + Redis instance
  for isolation, but each instance costs only ~100-150MB RAM (`shared_buffers`
  tuned down + Redis ~5MB) — roughly one agent's own resident cost, not a
  per-cluster tax that compounds with fleet size.
  [`future/infra/embedded-per-cluster-data-plane.md`](../../future/infra/embedded-per-cluster-data-plane.md).
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
  round trip. [`okf/mcps/mcps.ava.okf.md`](../../okf/mcps/mcps.ava.okf.md).
- **Fixed, small per-agent connection budget** — 2 pooled Postgres
  connections (shared with the LangGraph checkpoint saver) + one Redis
  subscription per agent, with pgbouncer transaction pooling in front of the
  cluster's Postgres so the connection count does not scale 1:1 with fleet
  size (hosted mode replaces per-agent pools with one bounded workload pool and
  one fixed four-connection control pool for the runner).
  [`agent/db/docs/db.ava.okf.md`](../../agent/db/docs/db.ava.okf.md).

None of this claims memory stops mattering — it is the honest current floor.
The next walls once memory is handled: the heartbeat's
wake-rate ceiling (~1.67/s, ≈750 agents on today's numbers) and LLM turn cost,
which is linear in fleet size regardless of any of the above.

### Optional rendered-page inspection

`scripts/post_deploy_visual/check.py` is an optional inspection tool. It grants
no production access and is not a release gate. Choose an authorized target;
the deployment owner supplies any authentication and notification policy.

Run `--check --base-url <frontend-entry-url> --health-url <gateway-origin-url>`.
The frontend URL must serve the UI, while the health URL names the gateway:
the script appends `/api/health` and rejects a frontend URL serving gateway
health JSON. It uses repo-pinned Playwright Chromium and writes browser probes,
metadata and capture artifacts. Use `--output-root <artifact-directory>` for
the target; choose the monitoring schedule as part of the deployment policy.

If authentication is needed, `AVA_VISUAL_GATE_COOKIE_FILE` accepts a mode-0600
Playwright storage-state JSON, Netscape cookie jar or `name=value` file. Protect
that file as a credential and revoke a leaked session through the target's
supported logout flow. No command implicitly accepts a new visual baseline;
`--accept-wave <sha> --accepted-by <reviewer>` records an explicit acceptance.
The output's escalation policy is diagnostic; an operator decides its use.

### Start / check / restart

The CLI owns local lifecycle; `ava start` initializes a fresh home and resumes an
existing one through the same path. First-start inputs persist before native
effects. Repeated start checks the same identity and selected service roster.

```bash
ava start
ava status
ava stop -y
ava restart
ava cluster status
ava cluster destroy [--drop-db]
```

Full `stop` closes infrastructure, browser and persistent PTYs; `--keep-infra` and
repeated `--keep-service` preserve explicitly selected resources. `restart` is the
ordinary local stop/start path and keeps infrastructure, browser and persistent
PTYs. Destroy decommissions this host's cluster: it stops it, retires the host's
native jobs (launchd, crontab, the Linux boot unit, the permissions helper) and marks the home
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
global population: the probe still exits 1 and grades that outage.

The probe does not alert. Every run emits `health_probe_ran` (INFO, healthy or not),
and every failed check emits `health_probe_failing` (check, failure class, message) on
each run it stays failed; Grafana rules do the rest: `ava-ops-health-probe-warning`
(`for: 3m`) and `ava-ops-health-probe-error` (`for: 10m`) per check, and
`ava-ops-health-probe-silent` when no `health_probe_ran` reached Loki for 15 minutes
(the probe stopped, or the event path did). A deploy window is silenced by the fleet
update's silence, except disk pressure; `gateway_liveness` and the dead-man rule also
reach Telegram directly, because the webhook cannot report a down gateway.

The OS job and CLI probe only observe. They do not invoke rollback,
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
  (`docs/postmortems/0008`) — leaves the existing plist untouched, and lets the
  next external converge apply any pending spec change.
- **`AVA_OS_JOBS_ENABLED=false` disables registration for a process.** The
  scheduler is one namespace per OS user, so a test-scoped `$AVA_HOME` cannot
  isolate it — the pytest suite sets this and `tests/fixtures/provisioning.py` fails any run
  that leaves a job behind. Deregistration is never gated. Operators do not set
  this: a prod cluster with it off silently loses its health probe,
  daily log maintenance, and its ability to come back after a reboot.

`ava stop`, restart and update use the native maintenance primitives.
Restart retains infrastructure and persistent PTYs; default stop closes those local
resources. Durable agent identity and work remain on disk.
A stop timeout is a failure; force escalation requires an explicit option.
Normal `ava start` resumes only after readiness. See the
[stop procedure](graceful-maintenance.md) for partial stop, coordinated
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
  fetches NEW on every host, then — still before anything stops, and also under
  `--dry-run` — runs NEW's `ava schedules verify` against the running gateway's
  schedule table (every stored script, agent-written ones included) from a
  throwaway worktree of NEW at `$HOME/.ava/pre-update-verify` on the host's current
  interpreter. The table is read by the home's own (OLD) source checkout — the only code
  the database authority admits — and handed to the worktree as a rows file
  (`--rows-file`), so NEW checks it offline and never dials the database. A red row (a moved module, an undefined name, a call that no longer binds), or a check that
  could not run, refuses with exit 2, the rows listed and nothing stopped; fix the
  scripts and rerun, or pass `--allow-red-schedules` to proceed (the rows then
  crash-loop after `up` until fixed). A host that has not fetched NEW (a dry run does
  not fetch) is reported as not checked. Then, before the first stop, it opens the window's
  alert silence (below). Then it runs `ava stop -y --timeout 600` on each runner and
  then the gateway (each must leave phase `stopped` with no failures), then on
  every host checks out NEW detached, repairs legacy read-only venv
  directories, runs `uv sync --frozen` and requires a clean tree.
- Runners fetch through the gateway's source clone (their checkout's `origin` is the gateway host's `~/.ava/source`): the target commit is fetched by SHA before it is reachable from any of the clone's refs, so the clone carries `uploadpack.allowAnySHA1InWant=true` (repo-local; set 2026-09-30). A reclone of the clone must re-apply it.
- A failed stop prints the next step: on that host retry
  `ava stop -y --timeout 600`, confirm `ava status` shows
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
  address and bearer in place; then each host runs `ava packages refresh`
  (skills follow their channel; `ava skill update` is retired) and its summary
  line (`summary: …`) is printed. A differing local copy is replaced and
  reported — an info line names the differing files. Last, two read-only drift checks
  run, each to the end before `up` fails with the details in the
  log: `ava schedules verify --no-notify` on the gateway (the stored schedule scripts, as
  above) and `ava plugins verify` on every listed host (each enabled plugin loads as an
  agent boot loads it; the loader skips a broken plugin, so this is what turns that into
  a failure — 2026-10-03: out-of-repo plugins calling deleted hook APIs). They come
  last so a red never skips the smoke or the refresh. Only after they pass does `up`
  expire the window's alert silence.
- The window is quiet through one Grafana silence, not through any gate in the services:
  `down` has the gateway host create a silence in the co-located Grafana's Alertmanager
  (`createdBy` `ava-fleet-update`, matcher `alertname=~".+"` except the disk alerts (`metric="host_disk"`, `attributes_check="disk_usage"`: a filling disk still pages) — comment naming NEW, expiry
  `--silence-hours`, default 4) before anything stops, and `up` expires it after the drift
  checks. A silenced rule keeps evaluating, so a condition that outlives the window notifies
  when the silence ends. A rerun of `down` extends the silence it owns; a failed `up`
  leaves it to its expiry (the cluster stays quiet while you fix the cause); `down --dry-run`
  only prints the command. The program (`cli/fleet_alert_silence.py`) ships on stdin, so the
  first update carrying it already has it; a host without `GRAFANA_ADMIN_PASSWORD`, or a
  Grafana that does not answer, prints a `WARNING: alert silence not open/close` line and
  the half goes on (a window without a silence is noisy, not unsafe). Expire a stuck silence
  by hand in Grafana (Alerting > Silences) or with `python - close` from that file.
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

### Release steps: retiring the `milvus` port slot (one-time)

The release that deletes the milvus memory-search backend drops the `milvus` slot (19530)
from the fixed port table, so every gateway home's start intent must lose that key before
any command of the new code (see the rule above). A runner-only home has no reservation
and needs nothing. The step is idempotent (it pops a key only when present).

1. **Between `down` and `up`, on every gateway home**, drop the slot. The file is compact
   JSON with sorted keys, mode 0600:

   ```bash
   python3 - <<'EOF'
   import json, os, pathlib
   home = pathlib.Path(os.environ.get("AVA_HOME") or pathlib.Path.home() / ".ava")
   path = home / "start-intent.json"
   data = json.loads(path.read_text())
   data["record"]["ports"].pop("milvus", None)
   staged = path.with_name(path.name + ".staged")
   staged.write_text(json.dumps(data, sort_keys=True) + "\n")
   staged.chmod(0o600)
   staged.replace(path)
   EOF
   ```
2. **Nothing else is required.** `AVA_MILVUS_PORT` / `AVA_MILVUS_URI` are retired settings
   ([decision](../decisions/2026-10-03-memory-search-drop-milvus.md)): their `.env` lines are
   removed by the 2026-10-03 env sweep, and a leftover `.env` line is ignored — every
   settings model ignores a key it does not declare. A `~/.ava/milvus-data/` directory, if
   the home ever ran milvus, is dead data and may be deleted.

### WAL-G archiving

See [component operations](../../services/backup/walg/docs/operations.md).

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
interfaces and proxies the app (`services/entrypoints/gate`). Any private-network device
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
[`docs/decisions/2026-06-11-multihost-deployment.md`](../decisions/2026-06-11-multihost-deployment.md)
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
[`scripts/data_plane_ops/rotate_cluster_secret.py`](../../scripts/data_plane_ops/rotate_cluster_secret.py)
(`--execute`) only after a bearer leak. Rotating the secret never touches the
logical-backup passphrase `$AVA_HOME/backups/logical-backup.passphrase`: a gateway
home's birth mints and pins it, independent of the secret; a home born earlier carries
`sha256(secret)`, pinned once, so every earlier logical backup keeps decrypting (an
empty-secret home carries a minted one instead). The script verifies the pin before it
writes the new secret, and journals each step with fingerprints, never secrets.
**The pinned file is backup-critical material**: nothing re-derives it, and losing it
makes every logical backup of the home unreadable; escrow a copy with the gateway's
backup keys ([decision](../decisions/2026-09-28-backup-passphrase-minted-at-birth.md),
[passphrase](../../services/backup/artifact/docs/passphrase.ava.okf.md)). Restart the
gateway, then issue every remote unit a new capability bundle (its telemetry token
derives from the secret). It does not change Postgres, Redis, ACLs, or PgBouncer.

Routine data-plane rotation is independent and uses
[`scripts/data_plane_ops/rotate_data_plane_secrets.py`](../../scripts/data_plane_ops/rotate_data_plane_secrets.py):

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
[`docs/decisions/2026-07-17-config-reducer-semantics.md`](../decisions/2026-07-17-config-reducer-semantics.md)):

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

## Code version gate

The [database code-version gate](../../base/db/docs/code-version-gate.ava.okf.md)
owns the process version, pooled-session admission, minimum update and exit
contract. Its [decision](../decisions/2026-09-30-client-side-code-version-gate.md)
records the boundary. The coordinated recovery order below remains an operator
procedure; it does not redefine the gate.

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

Event publication and payloads belong to
[the live event bus](../../base/events/live/docs/live.ava.okf.md);
interrupt and control belong to [the agent graph](../../agent/graph/docs/graph.ava.okf.md).

**Lifecycle command residue:** never hand-clean a stuck lifecycle command (a row
left pending/claimed, or a live `agents_meta.lifecycle_command_id` pointing at a
finished command) with an ad-hoc UPDATE. Settle it through the owning path — boot
recovery, the settle ops, or the repair script — and clear the pointer in the same
transaction: a manual flip to `done` is exactly the torn shape the commit-time
guard rejects (task #3678), and a manual pointer clear without a settle only makes
the failure invisible.



## Observability / Tracing

See [component operations](../../cli/commands/observability/docs/operations.md).

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
| an agent's exec subprocesses | `$AVA_HOME/logs/agent-{N}.log` (every exec subprocess of the agent appends) |
| raw session stdout (gateway / shells / daemons / schedules) | Loki (the LGTM backend): shell logs → `filelog/sessions`; gateway/daemon/schedule logs → `filelog/services`. Banner-only agent main stdout is excluded. All filelog receivers derive Loki `service_name` from the filename and persist offsets. Loki retains 84 hours; scheduled local cleanup uses the family tiers below. See `deploy/lgtm/README.md`. |

### Local log rotation and retention

See [component operations](../../cli/commands/observability/docs/log-maintenance.md).

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
[changed-files mode](../../scripts/lint/docs/changed-files-mode.ava.okf.md)). The
code-generation freshness hooks, whose check needs the whole project, run only
when one of the inputs in their `files:` filter changed. ESLint lints the changed
frontend files (`scripts/precommit-eslint.sh`) and the whole project when the
lint setup itself changed. A hook that scans the whole repository on every
commit regardless of the diff is a design error, not a price of the check; CI's
`pre-commit run --all-files` is the full scan of every hook and the merge gate.
Pre-push selectors share `scripts/prepush-base.sh`: the contribution is
`merge-base(origin/main, HEAD)..HEAD`, independent of a force-push's old remote
tip. Missing `origin/main` or a missing merge-base fails explicitly; fetch the
base before pushing. Frontend hooks always enter the selector, so deletions and
renames cannot disappear behind pre-commit's file filter. Upstream-only UI
changes inherited by rebase invoke no frontend tool.

`frontend-tsc` retains project typecheck and route generation for owned frontend
changes. `frontend-eslint-full` keeps its ID for CI compatibility but now checks
only surviving branch code paths through the existing warning gate. CI checks
whole-project lint effects. `frontend-vitest` runs changed tests and dependency-
related runtime source paths, plus known filesystem consumers (source-policy scan,
CSS/layout, messages, package binding, backend login HTML, event fixtures, plugin
vocabularies and app-UI locales). This is useful affected verification, not a
claim that an import graph proves dynamic or filesystem closure. Deleted inputs
and global configuration report the remaining CI-only verification explicitly;
no full-suite fallback runs locally. Empty related collection fails, including
Vitest's otherwise passing default for `related`. Diagnose a missing consumer
and run explicit affected tests; do not turn on `passWithNoTests` to certify
an empty collection.

Pyright checks the branch's surviving changed `.py` paths only
(`scripts/prepush-pyright-files.sh`); full-repository pyright and test suites
remain CI-only. Tool availability/load/lock skips still report missing evidence
through the existing guard; they are not proof that a check ran.
Other local hooks default to pre-commit; upstream hooks may also declare
pre-push hygiene checks. Run either stage explicitly with the worktree's own
environment:

```bash
.venv/bin/pre-commit run --all-files
.venv/bin/pre-commit run --all-files --hook-stage pre-push
```

The [testing guide](testing.md) describes explicit local test paths. The
[historical Vitest placement decision](../decisions/2026-09-24-vitest-prepush-selection.md)
retains its measurements and selection limitations; its full-local-suite policy
is superseded by the current scoped-local rule.

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
  would leave rebased commits unchecked at random). Missing scope is an error;
  missing `.venv/bin/pre-commit` still reports unverified local execution.
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
  a deleted path matches. A rename counts as deleting the old path; an unknown
  range fails explicitly. `types-codegen-fresh` alone skips when
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

The [GitHub Actions overview](../../.github/.github.ava.okf.md) owns workflow
composition, suite selection and hosted runners. The
[native test gate](../../.github/test-gate.ava.okf.md) owns result semantics;
[native CI infrastructure](../../.github/ci-data-plane.ava.okf.md) owns isolated
database prerequisites. [Testing](testing.md) owns scoped contributor checks.
Runtime operation and CI verification are separate: test and build processes
must not target a production home.

## Root-owned application lifecycle

[Host lifecycle](../../cli/commands/lifecycle/docs/lifecycle.ava.okf.md),
[start identity](../../cli/docs/start_identity.ava.okf.md),
[Ava Root](../../services/supervision/ava_root/docs/ava_root.ava.okf.md) and
[native closure](../../services/supervision/ava_root/docs/closure.ava.okf.md)
own service selection, launch generations, platform ancestry, readiness and
stop/destroy custody. Consult these owners before changing a running generation;
missing native evidence cannot establish readiness or exit.

Root framework tests under `services/supervision/ava_root/tests/test_ava_root_*` exercise supervision,
custody, IPC and readiness; an isolated native cluster is still required to verify
platform ancestry and actual application execution. There is no local branch-preview
controller: validate a branch with CI, the throwaway test clusters, and the Linux
verification container (`python3 scripts/verify/container.py --ref <ref>`: a fresh
container, the commit cloned to `~/.ava/source`, `ava init` and the first `ava start`, a scripted agent;
[verification boundaries](../../future/infra/verification-boundaries.md)); what only macOS can show
(the signed helper chain, the desktop grants) runs in a throwaway Tart VM cloned from the golden
image (`python3 scripts/verify/tart_run.py --ref <ref>`, same flow, plus the helper chain check).
