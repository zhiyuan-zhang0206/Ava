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
  its notification and optional message, then publishes a host wake.
- Automatic resurrection requires actual pending work newer than the current
  death and above the force-terminate inbound fence. The home-machine lock,
  automatic-wake policy and suppression window guard that transition. A stale
  trigger cannot undo a concurrent user termination.
- Host crash recovery reconciles stale owned rows and durable checkpoints;
  it does not launch individual agent processes.

| Operation | Host task | Agent ID and context | Persistent shells |
| --- | --- | --- | --- |
| `terminate()` | finishes native control | retained, terminal | retained |
| `restart()` | completes, then new admission | retained | retained |
| `resurrect()` | scheduled for terminated identity | retained | retained |
| cluster `pause` / update | drains native controls | retained, resumable | retained |
| full cluster `stop` | drains then stops host | retained, resumable | closed |

Related: [[../lifecycle.ava.okf.md]] and [[shared/maintenance.ava.okf.md]].
