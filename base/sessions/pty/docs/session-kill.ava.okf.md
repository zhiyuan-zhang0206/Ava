---
type: doc
title: "PTY session kill — bounded best-effort closure"
description: "Close the known shell and terminal with bounded signals to known shell/foreground groups; known leftovers are diagnostic, not proof of complete process disappearance."
tags:
- base
- pty
- sessions
---

# PTY session kill

`process_groups.py` supplies bounded signaling for known shell/foreground
process groups. The service's `kill` request closes the known shell and PTY;
`closure.py` applies the shared closure to stop, shutdown and recorded crash
leftovers. Shell birth identity is checked before signaling. Operational
signal failures raise, and a known shell that survives fails closure.

No host process-table scan, descendant walk, SIGSTOP freeze, session-id
freshness inference or periodic member census is part of this contract.
Background jobs, reparented descendants and detached processes may remain.
Their absence is not certified by a successful terminal closure.

`interrupted` describes observed work interrupted by closure. An absent/dead
session is an idempotent noop. Wire survivors list only known identities
observed alive; an empty list never proves every process disappeared.
`ava stop` reports known job leftovers without failing solely on their behalf;
a surviving shell still fails the stop. Owner notices describe shell/terminal
closure and work interruption rather than complete process destruction.

Residual host processes or OS stalls require agent/user investigation, with
identity and ownership checked before explicit action. No automatic host-wide
kill or reboot follows from this result. Decision:
[PTY best-effort closure](../../../../docs/decisions/runtime/processes/sessions/2026-10-07-pty-best-effort-closure.md).

## Dependencies

- [[pty_sessions/pty_sessions.ava.okf.md]] — the service and session lifecycle.
