---
type: doc
title: Host lifecycle
description: '`ava start` / `pause` / `stop` / `restart` / `maintenance` / `status` and the single application root they drive: one package, because every verb goes through the same root driver and drain boundary.'
tags:
- cli
- lifecycle
---

# Host lifecycle

## What it is

`cli/commands/lifecycle/` holds the local host lifecycle verbs and the machinery
they share. The verbs are `start.py` (`ava start`), `stop.py` (`ava pause`,
`ava stop`, `ava restart`), `maintenance.py` (`ava maintenance ...`) and
`status.py` (`ava status`). Every one of them reaches the single application
root through `root_driver.py`, and every stopping verb goes through the same
native drain boundary (`_temporary_stop`, `service_stop`) — which is why they
form one package rather than one per verb.

`root_driver.py`, `service_stop.py`, `start_generation.py`, `maintenance.py` and
`migrations.py` are public: `data_plane/maintenance_stop` and the cutover
scripts reach them. The `_`-prefixed modules are steps only this package calls.

## Stop, pause and maintenance

`stop.py` exposes `pause` and `stop` through `_temporary_stop`; restart reuses
its native drain, after `_start_readiness_preflight` has refused any restart
whose start would fail ([[start-readiness-preflight.ava.okf.md]]).
`ops.agent_pause` and `ops.agent_pause.probe` own prepare/drain and runtime
capability checks; `service_stop` and `data_plane/maintenance_stop` verify
resource exits (`_maintenance_stop_report` names survivors).
`data_plane/_pooler_stop.OwnedPooler` owns ordinary pooler stop
admission for maintenance and startup recovery: exact native birth and listener
proof precede a durable stop intent and the first SIGINT (`WAIT_FOR_SERVERS`).
Retries and already-closed listeners only wait; PgBouncer would interpret
another shutdown signal as immediate termination. A birth that finishes its
drain and exits while its listeners are being scanned is stopped, not a foreign
listener. An explicit force request alone permits a kill, with a separate
bounded settle wait when the graceful deadline is spent. The data-plane stop
requests [owned PostgreSQL](../../../../base/cluster/docs/postgres.ava.okf.md) fast
shutdown (SIGINT), so neither waits on idle client connections a drained state
cannot protect (issue #2307). When the data-plane phase still fails after the
services phase stopped, `_temporary_stop` compensates with a bounded internal
`ava start` (restoring services only when native storage admits startup)
instead of leaving the unit dark; the stop report and journal record the
outcome (issue #2307).

`_stop_extras` uses the same exact-home helper retirement as destroy: native
job, executable, socket and stopped-root custody are checked before native
helper exit and removal of its definition. Start recreates that definition.
Root owns Gate and native LGTM application services. `_pause_resume` releases
normal startup admission only after readiness, and never releases the fleet
cutover's hold (`cli/cutover_hold.py`, deleted with the cutover scripts);
`ava maintenance resume` refuses that hold and names its one exit,
`scripts/cutover_adopt_home.py --resume`.

`cli/parsers/maintenance.py` retains explicit intermediate steps through
`maintenance.py`, which reads its generation's hold through the maintenance
journal's own door (`base.deploy.maintenance.admission.require_operation`) and the agent-host
probes from `ops.agent_pause.probe`. They reuse the
[durable maintenance journal](../../../../base/deploy/maintenance/docs/maintenance.ava.okf.md).
See [the coordinated operator procedure](../../../../conventions/graceful-maintenance.md).

## Start

`cli/commands/lifecycle/migrations.py:cmd_migrations_apply` is deliberately
not a user-facing verb — it runs as a step of `ava start`, so any restart
crossing a schema change catches the DB up on its own. `_start_bookmarks` records the running source
after a successful start. What start treats as already-up, what it waits for,
and when an unready service becomes exit code 4:
[[start-readiness.ava.okf.md]].

## Key Dependencies

- [[cli/commands/docs/commands.ava.okf.md]] — the command-package overview
- [[start-readiness.ava.okf.md]] — the launch guard, the root-owned readiness
  wait and the failure exit code
