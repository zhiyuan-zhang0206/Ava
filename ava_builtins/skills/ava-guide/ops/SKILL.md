---
name: ops
description: Operates Ava cluster lifecycle, updates, converge, channels, releases, resources, and sessions. Use when starting, stopping, inspecting, enrolling, updating, or releasing a cluster, or when diagnosing session and runtime layout behavior.
---

# Ava Ops — Cluster Lifecycle & Maintenance

This sub-skill covers the operational verbs: starting, stopping, updating,
and understanding the cluster's runtime layout.

## Cluster / Unit / Machine Model

Hold this mental model once and the commands stop looking arbitrary.

- A **cluster** is one logical deployment. It owns its **own** Postgres + Redis
  instance (under its `$AVA_HOME`, on per-cluster ports), one outward gateway,
  one block of ports. Your peers and you live inside one cluster; you all see the
  same database and message bus.
- A **unit** is one install on one box, under its own home directory. A unit
  carries a set of **capabilities**: `gateway` (owns the data plane + the HTTP
  gateway) and/or `agent-runner` (runs agent processes). A single box usually
  carries *both*.
- A **machine** is a named box in the cluster. Most clusters are a single box
  (it carries both capabilities). The network posture is uniform — a single box
  is just the special case where the only reachable address is loopback, not a
  separate mode you switch on. Each machine carries a free-text description of
  what it is for, so when a task needs a capability your box lacks you read the
  other machines' descriptions (`ava.agents.list_machines()`) and spawn a peer
  there.

## Data Plane Discipline

Each cluster owns its **own** Postgres + Redis instance under its `$AVA_HOME`
(on per-cluster ports), driven directly via `pg_ctl` / `redis-server` — not
shared brew/systemd services, and not one instance partitioned by database name.
`ava start` ensures this cluster's pair is up (skip-if-running); `ava stop` tears
it down. Because `ava stop` (gateway role) takes this cluster's data plane down,
stop a dev worktree cluster with `ava cluster down --path <home>`, not a bare
`ava stop`.

Redis auth has two users, and confusing them is what turns an auth error into a
self-inflicted outage:

- `default` (admin) — its `requirepass` is the gateway-only
  `AVA_REDIS_ADMIN_PASSWORD`. It exists only to provision the runtime ACL user
  and run admin probes.
- the cluster's **ACL user** — your runtime identity (mirrors the per-cluster
  Postgres role), authenticating with its separate runtime password embedded in
  `AVA_REDIS_URL` and scoped to this cluster's keys + channels. It is
  **runtime-provisioned, not persisted**: only `requirepass` lives in
  `redis.conf`, so a bare `redis-server` restart brings redis back with the ACL user *gone*, and runtime connections fail with
  WRONGPASS / NOPERM until it is re-created.

So an auth error from inside your cluster is *the dropped ACL user*, not a
rotated password. The repair is `ava start` — it re-affirms the ACL user
idempotently. **Never restart redis (or Postgres) at the OS level to "fix" a
cluster auth error**: it drops the very ACL user that was failing, and on the
prod box it is a production outage. If `ava start` does not converge, escalate
instead of experimenting on the data plane.

## Start / Stop / Status

```bash
ava start     # pure bring-up (idempotent). Ensures this cluster's own pg/redis
              # instance, then brings up the union of this host's services. The
              # cluster is born at install time (scripts/install.sh), not here;
              # the home resolves from the checkout, never a flag.
ava pause     # normal agent drain; keep infrastructure, browser and persistent PTYs
ava stop      # normal drain, then full local stop; durable data and agent IDs survive
              # --keep-infra / --keep-service retain resources; --force is explicit
ava start     # after a pause/stop, restore services and resume after readiness
ava status    # check status (includes the pg/redis view)
```

For coordinated downtime and recovery, use the shared
[pause/stop procedure](../../../../conventions/graceful-maintenance.md).

**Bring-up ordering is strict.** Agent processes are never started directly —
they are always created through the gateway (`POST /api/agents`, which
`ava.agents.spawn` / the frontend / `scripts/start_agent.py` all share). So
**start the gateway first, then start agents**; a spawn issued before the
gateway is up has nowhere to land.

### Cluster sub-commands

```bash
ava cluster ls                        # list all registered clusters (label = home basename)
ava cluster status                    # full multi-machine roster
ava cluster down --path <home>        # stop the cluster at a home path, keep its slot + data
ava cluster destroy --path <home>     # stop + free registry slot + deregister its OS-scheduled
                                      # jobs (refused for ~/.ava, the prod home)
                                      # add --drop-db to also remove its pg/redis data dirs
```

### Split deployments (`ava enroll`)

A pure agent-runner on another box **enrolls** into an existing cluster instead
of birthing one of its own — it inherits the cluster's identity (db / redis /
channels) from the gateway:

```bash
printf 'Cluster secret: ' >&2
IFS= read -rs AVA_CLUSTER_SECRET
printf '\n' >&2
export AVA_CLUSTER_SECRET
ava enroll --gateway <URL> --machine-name <NAME> --machine-host <HOST>
unset AVA_CLUSTER_SECRET
# then: ava start
```

Enrollment presents the cluster secret (`AVA_CLUSTER_SECRET`) to the gateway's
authenticated `/api/bootstrap`, which returns the cluster's connection bundle
(db / redis URLs, channels). The runner's database URL carries a separately
minted least-privilege `ava_runner` password and its Redis URL carries the
runtime ACL password. The cluster secret remains the HTTP bearer only; the
gateway keeps independent Postgres-owner and Redis-admin credentials.
`--machine-host` is the runner's own reachable address (how the gateway dials
back to its ops server) and is **required**. The runner starts no gateway
process of its own; it needs both network reachability to the gateway *and* the
cluster secret.

## Update & Converge

`ava cluster update` is the capability-dispatched upgrade command:

- On a gateway-capable host (incl. single box): orchestrates the whole cluster —
  pause agent-runners → local pull/`uv sync`/migrate/restart → trigger
  agent-runner self-updates.
- On a pure agent-runner: self-updates (git pull + `uv sync` + restart).

Related commands:

- `ava restart` — restart all services on **current** code (no pull/sync/migrate).
- `ava cluster restart` — restart the whole cluster, same code.
- `ava converge` — re-apply idempotent host wiring (`ava` symlink, PATH,
  home directory, plugin images, memory pool). Runs automatically on every
  `ava start` / `ava cluster update`; run standalone if wiring looks off.

## Channel (update track)

A cluster tracks a GitHub branch as its update source — its **channel**,
controlled by `AVA_TRACK_BRANCH` (default `main`).

| Channel | Value | Who should use it |
|---------|-------|-------------------|
| Production | `main` (default) | All clusters |

```bash
ava config get AVA_TRACK_BRANCH      # view current channel
ava config set AVA_TRACK_BRANCH=<b>  # switch channel (then `ava cluster update`)
```

Switching channel only declares intent — run `ava cluster update` to actually pull.

Branch model:

```
feature/*  ──→ main  ──→ tag  ──→ ava cluster update   # the ONLY update entry point
```

## Update — operator CLI only

Use the installed `ava cluster update --help` contract, not historical drain
timeouts or restarter assumptions. Record the actual phase, elapsed time,
desired service state and hosted/process ownership. Forceful interruption
requires scoped authorization; a short configured drain is not a promise
about total downtime or successful convergence.

## Update Safety Discipline

- **Merge is not runtime health.** CI and exact-head review establish repository
  evidence, not successful deployment. Require explicit operator authorization,
  a fixed target, compatible schema/plugins/protocols and verified recovery
  evidence. Verify actual running services and representative agent progress
  after rollout; skipped checks or a filtered roster are not sufficient.
- **The prod checkout stays on `track_branch`.** It is the tree the live
  processes run from. Sitting on a feature branch means the cluster is running
  unreviewed code, and the next `ava cluster update` force-checkouts `track_branch`,
  **discarding any unmerged commits on it**. Develop in a worktree; never switch
  the prod checkout's branch by hand. `ava status` warns when the prod checkout
  has drifted off `track_branch`.
- **Recovery uses official lifecycle surfaces.** Preserve logs and inspect the
  installed `ava cluster recover --help` / `ava cluster rollback --help` contract.
  Check live holder semantics and schema compatibility. Never reset production
  source, reinstall its venv, send raw signals, or blindly retry a rollout.
  Escalate if no supported safe recovery path exists.
- **Old code drives the first rollout.** A newly merged safeguard does not
  protect the update that introduces it. Read the old orchestrator and record
  the bootstrap plan before activation; see `conventions/defensive-patterns.md`.
- **One operator.** Contributors and reviewers do not launch concurrent
  rollouts. Respect explicit CI-only/no-local-cluster requirements. Preparation
  and backup uploads should remain outside maintenance where supported, without
  dropping backup or rollback gates to meet a downtime target.

## Release Cut

Release cut tags `main` at milestone points. Tool: `scripts/release_cut.py`.

```bash
.venv/bin/python scripts/release_cut.py daily     # daily patch bump
.venv/bin/python scripts/release_cut.py weekly    # weekly minor bump
.venv/bin/python scripts/release_cut.py catchup   # backfill missed days
# add --push to push tags
```

The full release strategy (versioning scheme, cadence, digest handling) is the
module docstring of `scripts/release_cut.py`.

## Resource Oversight (the SRE loop)

Every machine's OTel Collector sidecar scrapes the traditional SRE layer into
Prometheus: host CPU / memory / load / disk / filesystem / network everywhere,
plus `postgresql` and `redis` against the cluster's own data plane on a
gateway-capable unit. They live under `job="ava-infra"` with `host` (the OS
hostname / physical identity) and `machine_name` (the Ava roster identity)
labels. The Grafana dashboard `ava-ops-main` (its "Host & data plane" section)
groups by `machine_name` and is the view.

**There are no resource limits in the code, deliberately.** A saturated box
may be a runaway loop or a training job doing exactly what it was asked; which
one it is depends on machine specs and co-tenancy, which the framework cannot
know. So the operator's job is judgment over the data, not enforcement of a
constant:

1. Watch the axes — latency percentiles (LLM, gateway, turn), error and
   warning volume, host utilization, data-plane saturation.
2. When something is out of band, identify the consumer before acting.
3. Then choose: investigate, terminate idle agents to shed load, tell the
   user, or decide the machine is legitimately busy and leave it.

Alert rules (R8-R12: sustained CPU, memory pressure, per-volume disk
watermark, Postgres connection saturation, Redis memory) fire into the same
alerts table and IM pipeline as the application rules. Their thresholds are
deployment facts living in
`deploy/lgtm/config/grafana/provisioning/alerting/rules.yml` — a box whose
normal state trips a rule wants that file edited, never a special case in
framework code.

`ava status` still answers on a cluster with no LGTM backend: each machine row
carries one live CPU / memory / disk reading. That is a current value, not a
history — the history is Prometheus's, and there is exactly one of it.

When disk pressure comes from dead agents' workspaces, the disposal playbook is [workspace-cleanup](../workspace-cleanup/SKILL.md).

## Sessions

Ava's long-running processes (gateway, agent-runners, services, agent shells)
run as named sessions on the platform session backend — the native process
supervisor on POSIX (`shared.posixproc`), `shared.winproc` on Windows, and
per-session detached pty hosts for agents' interactive shells. Key facts:

### Session naming

- `ava-<service>` — a service daemon session (gateway, ops, im-bridge, ...)
- `ava-agent-<id>` — an agent main process session
- `ava-agent-<id>-shell-<n>[-<name>]` — an agent's shell sub-sessions (and
  `...-watcher` for background watchers)
- `ava-updater` / `ava-rollout` / `ava-cluster-restart` — orchestration sessions

### Per-cluster session records

Each session's record (pid, start time) lives at `<ava_home>/run/sessions/
<session-name>.json` (agent shells: `<ava_home>/run/pty/`); its combined stdout+stderr goes to
`<ava_home>/logs/<session-name>.out.log` (orchestration sessions additionally
tee to `<ava_home>/logs/{updater,rollout,cluster-restart}-<epoch>.log` on
POSIX). `ava cluster status` enumerates the same sessions. Raw session
output is queried in Loki, not tailed by a CLI: the collector's
`filelog/sessions` receiver admits only agent shell transcripts, while
`filelog/services` admits gateway/daemon/schedule stdout and excludes all
agent main logs; updater/rollout tees use `filelog/orchestration`. Loki's
`service_name` label is the filename-derived session name. Query via Grafana
Explore (LogQL), `logcli --addr http://127.0.0.1:3100`, or the Loki HTTP API;
local managed logs are pruned only when `ava logs retention` runs. No age flag
keeps the configurable 14-day global fallback; deployment jobs use
`--family-days` for 15d agent, 7d named-PTY shell, 30d gateway/ops/watchdog,
and 3d other service rotations. The command scans only `$AVA_HOME/logs` itself,
admits exact agent, named-PTY, and Loguru-rotation shapes (including underscores),
rejects symlinks, and skips open handles. Register it daily; see `deploy/lgtm/README.md`.

### Environment forwarding

The session backend hands the child a built env dict (`shared.session_env.
forward_env_dict`) — host-scope env only (machine identity, paths, health
ports, the gateway URL) for daemon/service sessions; the cluster-scope values
are NOT forwarded — the child re-sources them at its own boot (fetch on a
pure runner, own .env on a gateway host). Detached agent processes get
`ops.agent_launch.agent_spawn_env_dict` — bootstrap guide keys only, cluster
secrets dropped. Nothing secret ever rides an argv (issue #974).

### Shell sub-sessions outlive agent processes AND cluster updates

Agent shell sub-sessions are deliberately NOT torn down on agent exit — they
persist across terminate/restart/update so background work (Claude Code,
watchers, a long training run) outlives the process that started it. Each
session runs in its own detached host process, so no service stop, rollout,
or watchdog respawn can kill it; only its own `kill`, its shell exiting, or a
machine reboot ends it. Orphan sessions are reclaimed as a periodic
management task.

Full detail: `shared/session_env.py`, `shared/session_backend.py`, `cli/commands/logs.py`.
