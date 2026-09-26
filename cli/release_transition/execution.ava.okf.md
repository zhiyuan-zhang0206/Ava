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
