---
type: doc
title: "PTY closure and the crash ledger"
description: "The one terminal closure (bounded best-effort shell and known-group signaling) the pty-sessions service runs for a stop, a shutdown and a sweep, and the ledger that lets the next start close what a crashed service left running."
tags:
- base
- pty
- sessions
---

# PTY closure and the crash ledger

## Closure

`close_all` runs `closure.py` inside the service. It closes known shells and
terminals with bounded signaling to known shell/foreground groups. Shell birth
identity is retained. Signal failures raise and surviving shells fail closure;
known job leftovers are reported without themselves failing `ava stop`.
New allocations are refused during closure. No all-descendant disappearance
proof is made. See [[../session-kill.ava.okf.md|session kill]].

Native tests wait for the intended job's own readiness before signaling when
its signal dispositions matter. A transient login-shell child does not prove
that the intended job is ready.

## The ledger

The service alone writes `run/pty-sessions.json`, recording known shell birth
identities, plus any foreground leader explicitly captured at teardown. Ordinary
live entries carry only their shell; there is no periodic process-table census. A crash closes
masters but can leave a shell or job running. The next service start, or a
stop finding no service, applies bounded closure to recorded identities that
still match; it never reconstructs an entire descendant tree. A surviving known
shell remains recorded for a subsequent cleanup attempt; job-only leftovers do
not keep a terminal-presence gate. Busy closure
notices describe known lost work, not absence of every background process.
The instance lock prevents a second start from sweeping a live service's
sessions. Decision:
[PTY best-effort closure](../../../../../docs/decisions/runtime/processes/sessions/2026-10-07-pty-best-effort-closure.md).

## Crash diagnostics

The database-free service records start-time sweep outcomes in local logs.
It does not stage owner notices or replay a previous start's journal. Existing
`run/pty-close-notices.json` files remain untouched and can be inspected with
`ops.pty_close_notices.read_pending`; no automatic sender consumes them.
Explicit operator stops still make one best-effort notification attempt for
known closed busy shells. Those writes retain same-key dedup and never wake a
terminated owner.

## Dependencies

- [[pty_sessions.ava.okf.md|PTY sessions]] — the service and its lifecycle.
- [[../session-kill.ava.okf.md|session kill]] — the bounded closure guarantee.
