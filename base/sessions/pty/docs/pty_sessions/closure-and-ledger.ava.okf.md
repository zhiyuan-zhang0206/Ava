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

## The ledger

The service alone writes `run/pty-sessions.json`: each live shell's identity
and the members of its session last seen alive (refreshed every ten seconds
with one process-table scan). A crash closes the masters, which hangs up every
shell; a shell or job that ignores the hangup survives. The next service start
(or a stop that finds no service) closes exactly the identities the ledger
names that are still the recorded processes, through the same closure; a live
recorded member keeps the session id proven when the shell is already gone.
The sweep returns the busy sessions it closed in the closure's shape, so a later
change can notify their owners; today it logs them. A second service start is
refused by the instance lock, so it can neither sweep nor take over a live
service's sessions.

## Dependencies

- [[pty_sessions.ava.okf.md|PTY sessions]] — the service and its lifecycle.
- [[../session-kill.ava.okf.md|session kill]] — the membership and kill proof the closure uses.
