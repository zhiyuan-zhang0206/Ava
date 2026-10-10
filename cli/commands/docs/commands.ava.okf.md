---
type: doc
title: CLI Command Modules
description: One subpackage per `ava` command domain, plus the few `_`-prefixed helpers those domains share. Public modules define `cmd_*` handlers, wired by the argparse tree in `cli/main.py`.
tags:
- cli
- tool
---

# CLI Command Modules

## What it is

`cli/commands/` holds the `ava` command domains. `cli.parsers.build_parser`
composes settings-free domain builders. Agents/notices are owned by
`agents/parsers.py` beside `control.py`, `notices.py` and `timeline.py`.
`agents/impersonation_parsers.py` owns external-session and relay arguments beside
`impersonation.py` and `impersonation_relay.py`; other builders remain in
`cli/parsers/`. Each builder binds its own `_h_*` adapter,
which lazy-imports the runtime `cmd_*` implementation at dispatch. There is
no registry or plugin mechanism; the wiring is the parser.

`cli/commands/__init__.py` is an empty package door: no import work and no
re-exports, so `import cli.commands` loads nothing else, Settings included.
Each command module is its own door; parser adapters lazy-import
`cmd_*` from the module that defines it, and test seams patch there. Eight
subpackages hold the domains, each an independent package door:

- `agents/` — lifecycle control, notices, timelines, external-agent
  impersonation, the pty allocation freeze and the computer-use daemons
- `management/` — gateway-managed config, presets, schedules
- `extensions/` — plugins, skills, packages, MCP servers, memory; owns its
  converge steps (`materialize.py`, `skills_sync.py`, `external_skills.py`)
- `observability/` — native LGTM desired state, the OTel collector, trace
  shipping, logs; owns its converge steps (`lgtm_native.py`, `otel_collector.py`)
- `data_plane/` — per-cluster Postgres/Redis/PgBouncer bring-up, their verified
  maintenance stop (`maintenance_stop.py`), `ava backup walg check|run|drill|restore|status` and the start-time
  WAL-archiving warning (`walg.py`); owns its converge step (`pgbouncer.py`)
- `cluster/` — whole-cluster verbs, the health probe, cron, the registry
- `converge/` — the orchestrator (`host.py`), the step contract (`spec.py`),
  the warning-only start preflights (health, ports, ownership), the
  rendered-file guard, and host-wiring steps owned by no other domain
  (firewall, Redis bridge, OS jobs including the WAL-G daily tick job, the WAL-G
  install/validate step `walg.py`).
- `lifecycle/` — `ava start` / `stop` / `restart` / `maintenance` /
  `status`, the pending-migration step, and the single application root they
  drive: [[cli/commands/lifecycle/docs/lifecycle.ava.okf.md|Host lifecycle]].

`cli.commands.probe` owns service evidence, health-port occupancy checks and
readiness reports, with an explicit `__all__` contract. Its declaration predicate
stays local; tiering and source drift stay with their `base.deploy` owners.

`stop.py` exposes `stop` through `_temporary_stop`; restart calls the same stop
kernel with the data plane and browser kept; terminals are closed like in any
stop. `ops.agent_pause` and `ops.agent_pause.probe`
own prepare/drain and runtime capability checks; `service_stop` and
`data_plane/maintenance_stop` verify resource exits.
`data_plane/_pooler_stop.OwnedPooler` owns
ordinary pooler stop admission for maintenance and startup recovery: exact native
birth and listener proof precede a durable stop intent and the first SIGINT
(`WAIT_FOR_SERVERS`). Retries and already-closed listeners only wait; PgBouncer
would interpret another shutdown signal as immediate termination. A birth that
finishes its drain and exits while its listeners are being scanned is stopped,
not a foreign listener. An explicit
force request alone permits a kill, with a separate bounded settle wait when
the graceful deadline is spent. The data-plane stop requests [owned PostgreSQL](../../../base/cluster/docs/postgres.ava.okf.md) fast shutdown (SIGINT), so neither waits on idle client connections a drained state
cannot protect (issue #2307); one still running at its budget's end is ended by an
immediate shutdown and reported. When the data-plane phase still fails after the
services phase stopped, `_temporary_stop` compensates with a bounded internal
`ava start` (restoring services only when native storage admits startup) instead of
leaving the unit dark; the stop report and journal record the outcome (issue
#2307). `_stop_extras` uses the same exact-home helper retirement as destroy:
native job, executable, socket and stopped-root custody are checked before
native helper exit and removal of its definition. Start recreates that definition.
Root owns Gate and native LGTM application services. `_pause_resume`
releases normal startup admission only after readiness.
The hold journal is read through `ava status` and ended by `ava start`
(`lifecycle/hold_report.py`, `lifecycle/_failed_receipts.py`). They reuse the [durable maintenance journal](../../../base/deploy/maintenance/docs/maintenance.ava.okf.md).
See [the coordinated operator procedure](../../../docs/conventions/operations/graceful-maintenance.md).

Gateway data-plane startup (`data_plane/cluster_instance`, `data_plane/bringup`,
`data_plane/pgbouncer`):
[[cli/commands/data_plane/docs/data-plane-startup.ava.okf.md|Gateway data-plane startup]].

## Notes

- Gate is an ordinary gateway service selected into the root manifest. Planned
  root downtime includes its entry port; it has no separate OS job or detached
  launcher. See [[services/entrypoints/gate/docs/gate.ava.okf.md|Fleet UI Gate]].

- `agents/timeline.py` exposes the existing timeline API as `ava agents timeline`
  and its exact `context` alias. `agents/impersonation.py` manages explicit external
  requests, leases, inbox acknowledgments, local Python SDK attachment, and the
  one attested send (`send` — task #4102).
  `agents/impersonation_relay.py` forwards inbound availability to the owning external
  model session; `--codex-remote` routes to the app server holding a Codex thread
  without waiting for its external queue-store scan.
  Usage: [External agent impersonation](../../../docs/conventions/agents/agent-impersonation.md).

- Which cluster a command acts on comes from `cli/commands/_repo.py:_repo_root` — the
  checkout the running `ava` belongs to — never the current directory.
- What `ava start` treats as already-up, what it waits for, and when an unready
  service becomes exit code 4 are one subject, in
  [[cli/commands/lifecycle/docs/start-readiness.ava.okf.md]].
- A fleet update is `python -m cli.fleet_update` (`cli/fleet_update.py`): a down
  script and an up script per unit, gated by the code version. There are no
  updater shell chains or bootstrap/continuation commands.
- Host-level Application Firewall and Redis bridge wiring are one subject:
  [[cli/commands/converge/docs/converge-host-wiring.ava.okf.md]].
- Explicit editable-install inspection and write-window primitives are described
  in [[editable-install-guard.ava.okf.md]]. Ordinary start and converge never
  repair or reinstall a separate production virtualenv.
- `cli/init_intent.py` and `cli/start_intent.py` are routed **before** settings-gated
  imports in `main()`: `ava init` records complete home identity (and starts nothing),
  and `ava start` admits that identity, before runtime configuration loads.
- [[cli/commands/converge/docs/ownership_preflight.ava.okf.md]] names the
  warning-only ownership repair guard that runs before converge writes later
  host state.

## Key Dependencies

- [[cli.ava.okf.md]] — the CLI domain overview: verbs, cluster identity, idempotent first start
- [[cli/commands/extensions/packages/docs/packages.ava.okf.md]] — the `ava plugins` / `ava skill` / `ava mcp` package surface
- [[cli/commands/lifecycle/docs/lifecycle.ava.okf.md]] — start, stop, restart,
  maintenance and the application root
