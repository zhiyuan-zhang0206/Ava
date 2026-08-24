---
type: doc
title: CLI Command Modules
description: One module per `ava` subcommand, plus `_`-prefixed internal steps that only start / update call. Public modules define `cmd_*` handlers, wired by the argparse tree in `cli/main.py`.
tags:
- cli
- tool
---

# CLI Command Modules

## What it is

`cli/commands/` holds one module per `ava` subcommand. The argparse tree lives
in `cli/parsers/` (one module per command domain, each holding that domain's
subcommand builders and `_h_*` handlers); `cli/main.py` composes it and
dispatches via `set_defaults(func=)` to the module's `cmd_*` handler — there is
no registry or plugin mechanism, the wiring is the parser.

Two kinds of module live here, distinguished by filename:

- **public** (`start.py`, `stop.py`, `status.py`, `logs.py`, `update.py`,
  `cluster.py`, `agents.py`, `config.py`, `plugins.py`, `skill.py`, `mcp.py`,
  `memory.py`, `presets.py`, `schedules.py`, `trace.py`, `migrations.py`,
  `cluster_lifecycle.py`) — reachable from the command line.
- **internal** (`_`-prefixed) — steps `start` / `update` call, never dispatched
  directly: `_cluster_instance` (per-cluster pg+redis bring-up), `_converge` /
  `_converge_spec` (the step contract) / `_converge_os_jobs` (the OS-scheduled
  jobs) / `_converge_skills` / `_converge_firewall` /
  `_converge_permission_watcher` (idempotent host wiring),
  `_update_git` /
  `_update_orchestration` / `_update_agent_runner` / `_updater_lease` / `_update_recover` /
  `_gateway_ready` (the staged upgrade), `_probe`, `_setup`, `_session_lifecycle`, `_repo`, `_warmup`,
  `_pkg_source`, `_pgbouncer`, `_lgtm`,
  `_claude_code_plugin`, `_cluster_health` /
  `_cluster_rollback` / `_cluster_cron` / `_cluster_watchdog_probe`.

`cli/commands/migrations.py:cmd_migrations_apply` is deliberately not a user-facing verb —
it runs as a step of `ava start` / `ava update`, so any restart crossing a
schema change catches the DB up on its own.

## Notes

- Which cluster a command acts on comes from `cli/commands/_repo.py:_repo_root` — the
  checkout the running `ava` belongs to — never the current directory.
- What `ava start` treats as already-up, what it waits for, and when an unready
  service becomes exit code 4 are one subject, in [[start-readiness.ava.okf.md]].
- `update.py`'s Phase-B poll answers a `PollVerdict` per host — the stall verdict reads the host's `host_deploy_state` row (idle → OK; live lease → working; paused+expired / converging+no-lease → STALLED ×2). A POLL_* status plus
  the `last_updater_outcome` the runner reported on the probe that settled it
  (`ops.updater_outcome`, read off that host's own updater log and anchored to its
  pause flag so a previous update's log is reported as *no record* rather than as
  this one's; on Windows, where the supervisor appends every run to ONE log, that
  flag anchors twice — the updater echoes a per-run start marker and the tail is
  sliced at it, so a previous run's decline is not read as this run's verdict). The status alone stops one level short of what an operator needs:
  `POLL_STALLED` covers both a preflight that refused (nothing stopped, host still
  serving its old code) and an updater that died after moving the checkout, and those
  want opposite next actions. It carries no extra dial — the probe that proves the
  host stopped is the probe that says why — and changes no deploy behaviour.
  A refusal is told to re-run rather than to wait: its own watchdog cannot clear it.
- `_gateway_ready` is a **precondition**, not a phase: the rollout's Phase B makes
  every agent-runner depend on the gateway (each runner's preflight refuses to stop
  services it cannot then restart), and the local leg's start is deliberately
  un-gated, so `rc == 0` there does not mean the gateway is serving. The gate probes
  the same URL + endpoint + headers the runners will (`_repo`'s `probe_gateway_once`)
  so the two cannot disagree, exits early on the failures a wait cannot fix, and
  turns a bound expiry into `RolloutOutcome.INCOMPLETE`. It is the stronger of the
  two checks — off-box and authenticated, where the start's gate is loopback and
  roster-wide.
- `_gateway_ready` only answers for the **gateway**, so the local leg's other services
  need their own channel: its child `ava start` writes the refused session names
  to `$AVA_HOME/last_launch_failures`, and the orchestration reads it right after the
  leg returns (`shared/launch_failures.py`). A non-empty list downgrades the rollout to
  `RolloutOutcome.INCOMPLETE` with the sessions named in the aftermath block, and does
  not abort it — the gateway is serving and the agent-runners still need Phase B. A
  record rather than an exit code because the names cannot ride an integer and the
  parent's own roster is the pre-pull tree's. Prod's 2026-08-06 rollout is what its
  absence cost: `✗ failed to start ava-frontend`, `rc=0`, three dark minutes.
- **The orchestrating host is not one of its own Phase-B targets**
  (`_update_orchestration:_phase_b_targets`). A single box carries `agent-runner`, so
  it is in the rollout list and stays there for Phase 0's fetch and Phase A's pause —
  both idempotent with the local leg. Phase B is where that stops holding: the local
  leg has already checked this host out, migrated it and restarted it, so the
  `cluster_update` op is redundant, and its first act is killing the gateway the
  readiness gate has just blessed. That is what stranded two runners for a settle
  window on 2026-08-01 (issue #1151). Behind it, `_repo:_probe_gateway_or_die` gives a
  runner's preflight a bounded budget (`GATEWAY_PREFLIGHT_BUDGET_S`) before one refused
  dial becomes a decline — defense in depth for holes a rollout did not open, not a
  second half of the same fix. A lone single box therefore runs an empty Phase B and
  reports CLEAN, but is still put through the readiness gate: nothing else asks whether
  its gateway came back.
- `_converge_firewall` reconciles the per-binary Application Firewall manifest.
  Version-stamped Python, Postgres, Homebrew, browser, and observability paths mean
  an upgrade can orphan the old ALF identity while loopback keeps working — issue
  #949. The step adds and unblocks resolved manifest paths, then removes stale
  managed rules. Verified macOS 26.5 hosts accept these `socketfilterfw` mutations
  without elevation; other versions fall back to `sudo -n` and then an exact manual
  command without blocking `ava start`. `_gateway_ready` uses the same audit when an
  off-box probe fails. See [[shared/shared.ava.okf.md|Shared Libraries]].
- `_converge_permission_watcher` installs the gateway host's
  `com.ava.permission-watcher` LaunchAgent. An unchanged plist is a strict no-op;
  a changed plist is bootstrapped under the logged-in user domain. The process is
  outside the `ServiceSpec` roster because launchd owns its keepalive. See
  [[services/permission_watcher/permission_watcher.ava.okf.md]].
- `cli/enroll.py` and `cli/preflight.py` are routed **before** settings-gated
  imports in `main()`, so they work on a host with no usable config yet.
- `cli/mcp_server.py` is the third top-level module a verb routes to
  (`ava mcp serve`) rather than a `commands/` module: it is a long-running
  stdio server, not a command that renders and exits, and it pulls in the mcp
  SDK that no other verb needs. See [[cli/commands/packages.ava.okf.md]].

## Key Dependencies

- [[cli.ava.okf.md]] — the CLI domain overview: verbs, cluster identity, install-time birth
- [[packages.ava.okf.md]] — the `ava plugins` / `ava skill` / `ava mcp` package surface
- [[start-readiness.ava.okf.md]] — what `ava start` calls up: the launch guard, the
  readiness wait, and the waiver over its exit code
