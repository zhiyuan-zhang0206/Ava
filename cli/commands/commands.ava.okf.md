---
type: doc
title: CLI Command Modules
description: One module per `ava` subcommand, plus `_`-prefixed internal steps used by host lifecycle commands. Public modules define `cmd_*` handlers, wired by the argparse tree in `cli/main.py`.
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

Most command modules follow these two naming groups:

- **public** (`start.py`, `stop.py`, `status.py`, `logs.py`,
  `cluster.py`, `agents.py`, `config.py`, `plugins.py`, `skill.py`, `mcp.py`, `pitr.py`,
  `memory.py`, `presets.py`, `pty.py`, `schedules.py`, `trace.py`, `migrations.py`,
  `cluster_lifecycle.py`, `agent_timeline.py`, `impersonation.py`,
  `impersonation_relay.py`) — reachable from the command line.
- **internal** (`_`-prefixed) — steps host commands call, never dispatched
  directly: `_cluster_instance` (per-cluster pg+redis bring-up), `_converge`
  (step-table aggregation and execution) / `_converge_spec` (the step contract) /
  `_converge_steps` (early host and data-plane wiring) / `_converge_os_jobs`
  (the OS-scheduled jobs) / `_converge_skills` / `_converge_firewall` (idempotent host wiring) /
  `_converge_redis_bridge` (idempotent host wiring),
  `_probe`, `_setup`, `_repo`,
  `_start_gui_chain` (the macOS GUI-chain warning), `_ownership_preflight`,
  `_pkg_source`, `_pgbouncer`, `_lgtm`,
  `_claude_code_plugin`, `_cluster_health` / `_cluster_cron`.

`stop.py` exposes `pause` and `stop` through `_temporary_stop`; restart reuses
its native drain. `ops.agent_pause` and `ops.agent_pause_probe`
own prepare/drain and runtime capability checks; `_maintenance_stop` and
`_maintenance_data_plane` verify resource exits. `_pooler_stop.OwnedPooler` owns
ordinary pooler stop admission for maintenance and startup recovery: exact native
birth and listener proof precede a durable stop intent and the first SIGINT
(`WAIT_FOR_SERVERS`). Retries and already-closed listeners only wait; PgBouncer
would interpret another shutdown signal as immediate termination. An explicit
force request alone permits a kill, with a separate bounded settle wait when
the graceful deadline is spent. The data-plane stop requests [owned PostgreSQL](../../shared/cluster/postgres.ava.okf.md) fast shutdown (SIGINT), so neither waits on idle client connections a drained state
cannot protect (issue #2307). When the data-plane phase still fails after the
services phase stopped, `_temporary_stop` compensates with a bounded internal
`ava start` (restoring services only when native storage admits startup) instead of
leaving the unit dark; the stop report and journal record the outcome (issue
#2307). `_stop_extras` uses the same exact-home helper retirement as destroy:
native job, executable, socket and stopped-root custody are checked before
native helper exit and removal of its definition. Start recreates that definition.
Root owns Gate and native LGTM application services. `_pause_resume`
releases normal startup admission only after readiness.
`cli/parsers/maintenance.py` retains explicit intermediate steps through
`_maintenance.py` and `_maintenance_probe`.
They reuse the [durable maintenance journal](../../shared/maintenance/maintenance.ava.okf.md).
See [the coordinated operator procedure](../../conventions/graceful-maintenance.md).

Gateway data-plane startup (`_cluster_instance`, `_data_plane`, `_pgbouncer`):
[[cli/commands/data-plane-startup.ava.okf.md|Gateway data-plane startup]].

`cli/commands/migrations.py:cmd_migrations_apply` is deliberately not a user-facing verb —
it runs as a step of `ava start`, so any restart crossing a
schema change catches the DB up on its own.

## Notes

- Gate is an ordinary gateway service selected into the root manifest. Planned
  root downtime includes its entry port; it has no separate OS job or detached
  launcher. See [[services/gate/gate.ava.okf.md|Fleet UI Gate]].

- `agent_timeline.py` exposes the existing timeline API as `ava agents timeline`
  and its exact `context` alias. `impersonation.py` manages explicit external
  requests, leases, inbox acknowledgments, local Python SDK attachment, and the
  one attested send (`send` — task #4102).
  `impersonation_relay.py` forwards inbound availability to the owning external
  model session; `--codex-remote` routes to the app server holding a Codex thread
  without waiting for its external queue-store scan.
  Usage: [External agent impersonation](../../conventions/agent-impersonation.md).

- Which cluster a command acts on comes from `cli/commands/_repo.py:_repo_root` — the
  checkout the running `ava` belongs to — never the current directory.
- What `ava start` treats as already-up, what it waits for, and when an unready
  service becomes exit code 4 are one subject, in [[start-readiness.ava.okf.md]].
- Prepared release transitions are owned by [[cli/release_transition/release_transition.ava.okf.md]].
  The command package is a docstring-only marker; callers import actual definitions.
  There are no updater shell chains, bootstrap/continuation commands, or re-export facade.
- Host-level Application Firewall and Redis bridge wiring are one subject:
  [[converge-host-wiring.ava.okf.md]].
- Explicit editable-install inspection and write-window primitives are described
  in [[editable-install-guard.ava.okf.md]]. Ordinary start and converge never
  repair or reinstall a separate production virtualenv; retained images are
  verified in place by `StartRuntime`.
- `cli/start_intent.py` is routed **before** settings-gated imports in `main()`,
  so first start records complete home identity before runtime configuration loads.
- `cli/mcp_server.py` is the third top-level module a verb routes to
  (`ava mcp serve`) rather than a `commands/` module: it is a long-running
  stdio server, not a command that renders and exits, and it pulls in the mcp
  SDK that no other verb needs. See [[cli/commands/packages/packages.ava.okf.md]].
- [[pitr.ava.okf.md]] defines the PITR inspection surface and the archive →
  verify → retire guard for finite migration rollback snapshots.
- [[ownership_preflight.ava.okf.md]] names the warning-only ownership repair
  guard that runs before converge writes later host state.

## Key Dependencies

- [[cli.ava.okf.md]] — the CLI domain overview: verbs, cluster identity, idempotent first start
- [[packages.ava.okf.md]] — the `ava plugins` / `ava skill` / `ava mcp` package surface
- [[start-readiness.ava.okf.md]] — what `ava start` calls up: the launch guard, the
  root-owned readiness wait and failure exit code
