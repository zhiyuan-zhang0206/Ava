---
type: doc
title: Host lifecycle
description: '`meiva start` / `stop` / `restart` / `maintenance` / `status` and the single application root they drive: one package, because every verb goes through the same root driver and drain boundary.'
tags:
- cli
- lifecycle
---

# Host lifecycle

## What it is

`cli/commands/lifecycle/` holds the local host lifecycle verbs and the machinery
they share. The verbs are `start.py` (`meiva start`), `stop.py` (`meiva stop`,
`meiva restart`) and `status.py` (`meiva status`, with `hold_report.py` reading the
maintenance hold for it). Every one of them reaches the single application
root through `root_driver.py`, and every stopping verb goes through the same
native drain boundary (`_temporary_stop`, `service_stop`) — which is why they
form one package rather than one per verb.

`root_driver.py`, `service_stop.py`, `start_generation.py`, `maintenance.py` and
`migrations.py` are public: `data_plane/maintenance_stop` and the cutover
scripts reach them. The `_`-prefixed modules are steps only this package calls.

## Stop, restart and maintenance

`stop.py` exposes `stop` through `_temporary_stop`; restart calls the same stop
kernel with the data plane and browser kept (`keep_infra`,
`keep_browser`); terminals are closed like in any stop, once `_start_readiness_preflight` has not
refused it for a start that would fail ([[start-readiness-preflight.meiva.okf.md]]).
`ops.agent_pause` and `ops.agent_pause.probe` own prepare/drain and runtime
capability checks; `service_stop` and `data_plane/maintenance_stop` verify
resource exits (`data_plane/maintenance_stop_report` names survivors).
`data_plane/_pooler_stop.OwnedPooler` owns ordinary pooler stop
admission for maintenance and startup recovery: exact native birth and listener
proof precede a durable stop intent and the first SIGINT (`WAIT_FOR_SERVERS`).
Retries and already-closed listeners only wait; PgBouncer would interpret
another shutdown signal as immediate termination. A birth that finishes its
drain and exits while its listeners are being scanned is stopped, not a foreign
listener. An explicit force request alone permits a kill, with a separate
bounded settle wait when the graceful deadline is spent. The data-plane stop
requests [owned PostgreSQL](../../../../base/cluster/docs/postgres.meiva.okf.md) fast
shutdown (SIGINT), so neither waits on idle client connections a drained state
cannot protect (issue #2307). When the data-plane phase fails after the
services phase stopped, `_temporary_stop` reports the failure and exits non-zero;
it does not restart anything, and the operator decides whether to retry the stop
or run `meiva start`.

`_stop_extras` uses the same exact-home helper retirement as destroy: native
job, executable, socket and direct root-child ownership are checked before native
helper exit and removal of its definition. Start recreates that definition.
Root owns Gate and native LGTM application services. `_pause_resume` releases
normal startup admission only after readiness.

No command reads or ends a hold except `meiva status` (and `meiva status --json`, the
hold alone as one JSON object, settings-lite) and `meiva start`: the holds are taken
by `meiva stop` and `meiva restart` and released by `meiva start`. `_pause_resume`
releases the hold once the unit serves; a continuation that failed during the
drain was only logged and alerted by the agent host. These reuse the
[durable maintenance journal](../../../../base/deploy/maintenance/docs/maintenance.meiva.okf.md).
See [the coordinated operator procedure](../../../../docs/conventions/operations/graceful-maintenance.md).

The native stop tests create fresh PTY sessions and wait for their requested
job's bare readiness output before treating a terminal as busy. A login-profile
child can exist before the initial command runs; child presence alone is not
job readiness. `cli/commands/lifecycle/tests/stop/stop_support.py:busy_session` keeps cleanup custody of the
shell before waiting, then records the running job. Regular jobs print
`job-ready`; stubborn jobs print `stubborn-ready` after installing their signal
handlers. Fresh-session creation refuses reuse; the public capture wait requires
bare output rather than shell echo. Native closure, notice count and
stop-before-data-plane assertions remain intact.

The stop tests' private non-PTY sessions also use literal parent and child
entries. The launcher accepts only SIGTERM behavior (`default`, `exit`, or
`ignore`), an optional ignored SIGHUP, and an optional child armed-marker path.
These values travel as argv data; callers cannot supply executable source.
The ready marker follows signal setup, the child arms after its own handler,
and the fixture records native session identity before returning. Its context
retains each original process and closes that exact private group and pipes
when the test exits, including an assertion failure.

## Start

`cli/commands/lifecycle/migrations.py:cmd_migrations_apply` is deliberately
not a user-facing verb — it runs as a step of `meiva start`, so any restart
crossing a schema change catches the DB up on its own.

Every host runs that step on a cold start, so its two branches split by who can
mutate the schema. A plane this home owns migrates over the postmaster's own
owner socket — direct by construction, and it survives a transaction pooler
because the applier holds a session advisory lock for its whole loop. A plane this
home does not own is dialed ordinarily: an agent-runner only ever finds nothing
pending, because the mutation authority is checked exactly when something is. A
provider-managed plane's URL is that provider's only address for us — nothing here
can tell whether it is a pooler. The applier therefore accepts one residual risk: a
provider-side transaction pooler would drop the session advisory lock silently. No
such plane runs in this fleet today; if one is added, point the URL at the
provider's direct (session-mode) endpoint, or run migrations from a host that can
prove directness. The checkpoint schema check that follows both branches is a
read-only verification, so it dials the ordinary access URL as well.

`start_generation.record_running_sha` records the running source
after a successful start. What start treats as already-up, what it waits for,
and when an unready service becomes exit code 4:
[[start-readiness.meiva.okf.md]].

After a ready gateway, start prints the gateway hint and, through
`cli/commands/lifecycle/login_link.py:print_login_link`, a one-time UI login link
(`<ui>/#ticket=<ticket>`). The ticket is minted with the home's human secret and
only when output is a terminal and a UI service was launched; a failed mint is
reported on stderr and never fails the start. Gateway side:
[[gateway/http/auth/docs/web-sessions.meiva.okf.md]].

## Key Dependencies

- [[cli/commands/docs/commands.meiva.okf.md]] — the command-package overview
- [[start-readiness.meiva.okf.md]] — the launch guard, the root-owned readiness
  wait and the failure exit code
