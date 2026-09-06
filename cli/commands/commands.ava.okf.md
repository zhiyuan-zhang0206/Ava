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
  `cluster.py`, `agents.py`, `config.py`, `plugins.py`, `skill.py`, `mcp.py`, `pitr.py`,
  `memory.py`, `presets.py`, `pty.py`, `schedules.py`, `trace.py`, `migrations.py`,
  `cluster_lifecycle.py`, `agent_timeline.py`, `impersonation.py`,
  `impersonation_relay.py`) — reachable from the command line.
- **internal** (`_`-prefixed) — steps `start` / `update` call, never dispatched
  directly: `_cluster_instance` (per-cluster pg+redis bring-up), `_converge`
  (step-table aggregation and execution) / `_converge_spec` (the step contract) /
  `_converge_steps` (early host and data-plane wiring) / `_converge_os_jobs`
  (the OS-scheduled jobs) / `_converge_skills` / `_converge_firewall` (idempotent host wiring) /
  `_converge_gate` / `_gate_systemd` (per-home launchd or Linux user-systemd gate),
  `_converge_redis_bridge` (idempotent host wiring) /
  `_converge_legacy_permission_watcher` (one-shot cleanup of the removed
  permission-prompt watcher),
  `_update_git` /
  `_update_orchestration` / `_update_agent_runner` / `_update_uv_sync` /
  `_updater_lease` / `_updater_stage` (the cmd.exe ladder's per-step telemetry marker) / `_update_recover` /
  `_gateway_ready` (the staged upgrade), `_probe`, `_setup`, `_session_lifecycle`, `_repo`, `_warmup`,
  `_ownership_preflight`,
  `_pkg_source`, `_pgbouncer`, `_lgtm`,
  `_claude_code_plugin`, `_cluster_health` /
  `_cluster_rollback` / `_cluster_cron` / `_cluster_watchdog_probe`.

`cli/commands/migrations.py:cmd_migrations_apply` is deliberately not a user-facing verb —
it runs as a step of `ava start` / `ava update`, so any restart crossing a
schema change catches the DB up on its own.

## Notes

- The fleet UI gate stays outside service-session teardown. Linux uses a
  per-home user-systemd unit with crash restart; unchanged active units survive
  updates, while source-hash changes replace them after a completed stop.
  Full stop and destroy use that same home identity. See
  [Linux gate supervision](../../conventions/linux-gate-supervision.md).

- `agent_timeline.py` exposes the existing timeline API as `ava agents timeline`
  and its exact `context` alias. `impersonation.py` manages explicit external
  requests, leases, inbox acknowledgments and local Python SDK attachment;
  `impersonation_relay.py` forwards inbound availability to the owning external
  model session; `--codex-remote` routes to the app server holding a Codex thread
  without waiting for its external queue-store scan.
  Usage: [External agent impersonation](../../conventions/agent-impersonation.md).

- Which cluster a command acts on comes from `cli/commands/_repo.py:_repo_root` — the
  checkout the running `ava` belongs to — never the current directory.
- What `ava start` treats as already-up, what it waits for, and when an unready
  service becomes exit code 4 are one subject, in [[start-readiness.ava.okf.md]].
- The gateway/runner update boundary, readiness proof, Phase-B verdicts, and
  failed-update recovery are one subject in [[rollout-boundary.ava.okf.md]].
- A full agent-runner update checks out, syncs, and records the installed SHA in
  its pre-checkout image, then re-execs `_update_agent_runner` with its private
  post-checkout flags before validation, quiesce, stop, or start. The gateway's
  post-boot schedule-session bounce likewise runs `_update_local` in a fresh
  subprocess; neither path imports new-tree modules into a process with old
  `sys.modules` entries.
- `_converge_firewall` reconciles the per-binary Application Firewall manifest.
  Version-stamped Python, Postgres, Homebrew, browser, and observability paths mean
  an upgrade can orphan the old ALF identity while loopback keeps working — issue
  #949. The step adds and unblocks resolved manifest paths, then removes stale
  managed rules. These `socketfilterfw` mutations were empirically verified without
  elevation on the macmini running macOS 15.3.1; other versions fall back to
  `sudo -n` and then an exact manual command without blocking `ava start`.
  `_gateway_ready` uses the same audit when an off-box probe fails. See
  [[shared/shared.ava.okf.md|Shared Libraries]].
- `_converge_redis_bridge` installs the repo-owned pure-stdlib relay into
  `$AVA_HOME`, converges or retires its macOS KeepAlive job as the cluster shape
  changes, and exposes the authenticated Redis PING used by `ava status` and the
  alert-only cluster health check. The listener recreates its socket after an
  interface or descriptor failure; Redis itself never widens beyond loopback.
- The prod editable-install assertion and update write window are one lifecycle
  guard: [[editable-install-guard.ava.okf.md]]; the prod source checkout's
  integrity (periodic reset + probe detection) is its sibling guard:
  [[source-tree-guard.ava.okf.md]].
- `cli/enroll.py` and `cli/preflight.py` are routed **before** settings-gated
  imports in `main()`, so they work on a host with no usable config yet.
- `cli/mcp_server.py` is the third top-level module a verb routes to
  (`ava mcp serve`) rather than a `commands/` module: it is a long-running
  stdio server, not a command that renders and exits, and it pulls in the mcp
  SDK that no other verb needs. See [[cli/commands/packages.ava.okf.md]].
- [[pitr.ava.okf.md]] defines the PITR inspection surface and the archive →
  verify → retire guard for finite migration rollback snapshots.
- [[ownership_preflight.ava.okf.md]] names the warning-only ownership repair
  guard that runs before converge writes later host state.

## Key Dependencies

- [[cli.ava.okf.md]] — the CLI domain overview: verbs, cluster identity, install-time birth
- [[packages.ava.okf.md]] — the `ava plugins` / `ava skill` / `ava mcp` package surface
- [[start-readiness.ava.okf.md]] — what `ava start` calls up: the launch guard, the
  readiness wait, and the waiver over its exit code
- [[rollout-boundary.ava.okf.md]] — rollout child classification, gateway
  readiness, Phase B, and recovery authentication
