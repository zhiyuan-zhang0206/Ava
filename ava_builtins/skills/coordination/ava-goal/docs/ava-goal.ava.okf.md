---
type: doc
title: ava-goal skill — Sustain pursuit of a terminal goal
description: Evidence-based completion and continuity across turns, with freely chosen peer coordination. Direct execution, delegation, and mixed collaboration are available; watcher/worker supervision is an optional pattern, not an assigned topology.
tags:
- extensions
- agent-instruction
---

# ava-goal skill — Sustain pursuit of a terminal goal

## What it is
The `ava-goal` skill guides sustained work toward a terminal outcome. Reading it
does not assign the agent a watcher or worker role. An agent can execute directly,
delegate, review peers, or combine these responsibilities using existing
capabilities; no special framework support is required.

The skill owns the working procedure: acceptance evidence determines completion,
progress notes preserve continuity, and deliberate pauses carry a handoff and a
resume condition. Its optional supervision example uses messaging and a one-shot
idle watcher to review another peer's progress. The bundled `scripts/watch_idle.py`
implements that temporary wait; it is not required for direct execution.

## Boundaries
Use this method for work that can finish. Idle requests a review; it does not
prove failure or authorize continuation after a deliberate pause. Budget reminders
call for reassessment and preservation of results, not automatic termination.
Completion of a goal does not require termination of a persistent peer.

Perpetual trigger-driven roles belong in Ava Guide schedules. Review a single
round against that round's outcome without repeatedly pushing a correctly idle
recurring agent to continue.

## Key dependencies
- [[ava_builtins/skills/docs/skills.ava.okf.md|Skills index]] — full skills catalog
- [[ava/docs/watcher.ava.okf.md|Watcher SDK]] — temporary waits used by the optional supervision pattern
