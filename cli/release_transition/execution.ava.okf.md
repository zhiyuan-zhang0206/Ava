---
type: doc
title: Release transition execution
description: Phase-by-phase release effects, recovery direction, root start through the platform's persistent root owner and fresh readiness before resume.
tags: [cluster-lifecycle, release]
---

# Release transition execution

`execute.py` advances prepared, quiescing, stopping, selecting, starting,
observing, resuming, and complete. Each phase reconciles actual state. Candidate
start/observation failure chooses the captured predecessor once, closes the
candidate, then uses the same select/start/observe path. Recovery never chooses
another target or loops between releases. A failure while resuming retains
that phase: admission may already have opened, so it is not an automatic
rollback boundary.

The stop phase closes this unit's writers. Persistent terminals — agent
shells, coding sessions, watchers, page and schedule runners — do not survive
a release ([decision](../../decisions/2026-09-27-fleet-release-and-cutover-policies.md)
item 2). While root still serves them, the phase waits a bounded time for
their jobs to finish, signalling nothing. It then stops root, keeping
terminals, so no reconciler re-arms a session. `close_release_terminals`
(`cli/commands/maintenance_stop.py`) captures every recorded shell, job and PTY
host birth, records the `ava stop` closure notice for each busy session's
owner (naming the release, before any signal), HUPs shells and TERMs jobs,
and after a grace SIGKILLs only those captured births still live. Closure is
the kernel observation that each is gone and no recorded terminal is live;
selection checks that evidence again. A survivor fails the phase with its
identity. After start, the schedule manager re-arms schedules and the page
server re-arms open pages; other sessions stay closed, and their owners learn
it from the notice.

`local.py` owns those release effects and selects the native root owner by
the operation's recorded executor kind. On Linux `root_service.py` uses the
existing home boot unit, never a second application supervisor. Its
one-operation start action runs `stage.py` in that unit; the new root therefore
survives the finite executor's exit. On macOS the persistent home helper births
root: [[cli/release_transition/root_macos.ava.okf.md]]. Readiness checks every
selected service plus native root identity. Resuming checks fresh readiness
again, including after an interrupted executor or a previously written resume
receipt. A durable serving marker is not a substitute for a live observation.

`stage.py` admits the captured image and uses ordinary `run_start`. Release
startup does not mutate an editable checkout, install dependencies, migrate, or
silently prepare external assets. After observation, `boot.py` becomes the
steady pinned-image boot action on Linux; on macOS the keeper's pinned seed is. `shared/release_operation.py` blocks unrelated
ordinary startup while an operation remains incomplete. Only the exact current
starting journal revision grants the in-process start capability; child
processes do not inherit it.
