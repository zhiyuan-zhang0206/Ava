---
type: doc
title: "PTY allocation generation boundary"
description: "A freeze advances the generation immediately: reconcilers reap superseded exact sessions, current desired state remains rebuildable, and corrupt markers are repaired without discarding their generation UUID."
tags:
- shared
- pty
- sessions
---

# PTY allocation generation boundary

## Desired-state effect

A successful `ava pty freeze` changes the desired-state generation at the
freeze acknowledgement, not at `resume`. Allocation itself does not directly
kill a PTY, but reconcilers apply the boundary on their next pass:

- ScheduleManager reaps every earlier-generation schedule PTY on its next tick
  and records an open schedule run as `interrupted`. An enabled schedule remains
  current desired state and can later be rebuilt under the new generation.
- At the next agent boot, watcher reconciliation reaps each
  earlier-generation watcher row and retains it as `reaped` history. Its old
  `cron` declaration is not restored automatically. Reaped `at` and `launch`
  one-shots notify their owner that the event was missed.

An inspection freeze has the same boundary semantics. Fence reconcilers before
freezing when an operation requires selective cleanup rather than this complete
generation sweep.

## Corrupt marker repair

A corrupt allocation marker fails closed. Never delete it: absence changes the
current generation to `None`, which can make reconciliation classify every
generation-bound desired record as superseded. Recover the original generation
UUID from a known-live PTY session record, rebuild a valid marker with that
exact UUID, and then permit reconciliation. If no session record establishes
the UUID, leave allocation fail-closed until an operator makes the boundary
explicit.
