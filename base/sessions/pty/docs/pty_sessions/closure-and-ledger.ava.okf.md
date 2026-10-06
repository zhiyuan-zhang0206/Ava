---
type: doc
title: "PTY closure and the crash ledger"
description: "The one terminal closure (hang up, a grace, SIGKILL whole sessions) the pty-sessions service runs for a stop, a shutdown and a sweep, and the ledger that lets the next start close what a crashed service left running."
tags:
- base
- pty
- sessions
---

# PTY closure and the crash ledger

## Closure

`close_all` runs the one terminal closure (`closure.py`) inside the service:
capture each shell's session as `session_tree` defines it, HUP the shells and
TERM the rest, wait a bounded grace, SIGKILL what is left, session by session.
It returns the busy sessions whose shell it verified gone (with what of each
outlived the SIGKILL) and every process that outlived the SIGKILL.
`ava stop` turns the first into owner notices (`ops/pty_close_notices.py`) and
fails on the second; new allocations are refused for its duration. The same
closure runs at the service's SIGTERM and for the ledger sweep.

Native closure and service-crash tests wait for the job's own bare readiness
line before signaling when its signal dispositions are part of the precondition.
Typed-job tests match the intended child argv; arbitrary shell children do not
prove that the job or its handlers are ready. Dedicated fork-handler fixtures
retain their own readiness files.

## The ledger

The service alone writes `run/pty-sessions.json`: each live shell's identity
and the members of its session last seen alive (refreshed every ten seconds
with one process-table scan). A crash closes the masters, which hangs up every
shell; a shell or job that ignores the hangup survives. The next service start
(or a stop that finds no service) closes exactly the identities the ledger
names that are still the recorded processes, through the same closure; a live
recorded member keeps the session id proven when the shell is already gone.
The sweep returns the busy sessions it closed in the closure's shape, and also
those the crash ended whole (every process gone, a reboot included) that the
ledger last saw running a job: their owners lost it all the same. A second
service start is refused by the instance lock, so it can neither sweep nor take
over a live service's sessions.

## Crash notices

The service holds no database. After a start-time sweep it stages the busy
sessions' notices on disk (`run/pty-close-notices.json`) and gives them to a
one-shot child, `python -m ops.pty_close_notices`
(`services/agent_runner/pty_sessions/crash_notices.py`), from a task beside the serving loop
with a thirty-second limit; the child writes the whole batch in one transaction,
under `CRASH_REASON`, and removes the file. A batch the child does not finish —
the limit cut it short, or the database answered nothing — loses nothing: it
stays staged, is reported at ERROR with its count, and the next start re-sends
it on the same idempotency keys. A database that cannot be reached is still
never a failed or delayed start. A stop that finds no service closes from the
ledger and writes the same notices itself. Delivery is idempotent on machine,
agent, session and shell birth, and a terminated owner is dropped.
Decisions: `docs/decisions/2026-10-04-pty-crash-notices.md`,
`docs/decisions/2026-10-04-pty-crash-notices-staged-retry.md`.

## Dependencies

- [[pty_sessions.ava.okf.md|PTY sessions]] — the service and its lifecycle.
- [[../session-kill.ava.okf.md|session kill]] — the membership and kill proof the closure uses.
