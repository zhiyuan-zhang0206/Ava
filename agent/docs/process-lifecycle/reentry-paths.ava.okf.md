---
type: doc
title: Agent Re-entry Paths
description: Restart and resurrection retain identity while scheduling new hosted work.
tags: []
---

# Agent Re-entry Paths

Restart and resurrection both retain agent ID, context and workspace, but
accept different lifecycle states.

- Restart is an ordinary durable control. The current turn reaches claim, exits
  normally, flushes and applies its command; the next host admission creates
  the successor incarnation.
- Explicit resurrection changes a terminated identity to idle atomically with
  its notification, optional message and an epoch fence: every earlier
  unapplied restart/terminate command is settled as superseded, so a delayed
  older terminate can never be replayed onto the successor incarnation
  (issue #2158). Then it publishes a host wake.
- Ordinary fresh chat may resurrect a terminated owner only when the input is
  newer than the current death and above the force-terminate fence. Home placement,
  pause state and unresolved resource/lifecycle evidence still guard admission.
  Historical recovery markers grant no exemption. A permanent provider rejection
  stops background wakes; explicit new chat or operator resurrection may retry.
- Crash reaping records termination and diagnostics without enqueueing recovery
  work. The delivery watchdog neither harvests a corpse nor resurrects a stalled
  or terminated owner.
- Host boot reconciles stale owned rows and consumes interrupted claims. New work
  starts at claim with fresh input; it never resumes old code execution. Interrupted
  compact or native cancel remains held when completion is unknown.

| Operation | Host task | Agent ID and context | Persistent shells |
| --- | --- | --- | --- |
| `terminate()` | finishes native control | retained, terminal | retained |
| `restart()` | completes, then new admission | retained | retained |
| `resurrect()` | scheduled for terminated identity | retained | retained |
| `ava restart` / update | drains native controls | retained; interrupted work not replayed | closed |
| full cluster `stop` | drains then stops host | retained; interrupted work not replayed | closed |

Related: [[../lifecycle.ava.okf.md]] and [[base/deploy/maintenance/docs/maintenance.ava.okf.md]].
