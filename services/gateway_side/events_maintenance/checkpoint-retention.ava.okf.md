---
type: doc
title: Per-thread checkpoint retention
description: Retained keep-three checkpoint pruning implementation, unscheduled since the 2026-09-30 trim opt-in retirement under the never-delete ruling.
tags: []
---

# Per-thread checkpoint retention

## Contract

The daemon's checkpoint trim opt-in was retired on 2026-09-30 under the
never-delete ruling (2026-09-12, task #3180). No daemon schedules
`checkpoint_reaper.prune_threads`; live checkpoint history is retained. The
reaper implementation remains for a separate retirement decision. If called,
one table-driven grouping counts all checkpoint rows by `thread_id`;
threads above the fixed keep-three budget become candidates regardless of agent
status, liveness, or whether an `agents_meta` row exists. Compaction boundaries
are exempt from the budget: a checkpoint stamped `compact_boundary: true` is
never trimmed, and blob retention follows the surviving-checkpoint reference
rule — each past compaction segment stays recoverable as one full snapshot
(user ruling: preserve information, bound storage).

Each pass sorts and rotates the candidates using a wall-clock-derived offset,
then completes at most 64 productive trims. It rechecks a thread's row count
immediately before trimming, so stale candidates are skipped without consuming
the cap. Repeated passes are idempotent and rotate fairly across candidate sets
larger than one pass.

## Atomic trim and race guard

`base.agents.history.checkpoint_cleanup.trim_checkpoints_sync` performs each thread trim as
one PostgreSQL statement per bounded batch. The statement ranks checkpoint IDs
newest first, deletes rows outside the keep window with their writes, and keeps
blobs referenced by every checkpoint surviving that particular batch, including
rows awaiting a later batch. Each committed batch remains readable after an
interruption. Surviving checkpoints link to their nearest surviving ancestor
from the original chain, preserving fork access to retained compact segments.
Only version-4 full snapshots are eligible: earlier formats depend on their
parent's pending-send writes, and delta formats depend on the full replay chain.

An explicit operator can also supply `preserve_checkpoint_ids`, for example
segment endpoints verified from the compact version when a best-effort boundary
stamp was absent. These IDs supplement the latest window and stamped boundaries;
they do not schedule the retained reaper or change boundary metadata.

PostgresSaver can expose a new messages blob before its checkpoint row. The
trim therefore proceeds only when the newest checkpoint's exact referenced
messages blob exists and no blob with a higher numeric messages-version counter
is visible. The same guard gates blob deletion. A newest checkpoint without a
messages version has nothing to await and may be trimmed normally. A guard skip
does not consume the productive-thread cap, allowing later candidates to run.

## Ownership

- `services/events_maintenance/checkpoint_reaper.py` owns candidate discovery,
  rotation, recheck, and the keep-three policy.
- `base/agents/history/checkpoint_cleanup.py` owns the atomic trim, survivor references, and
  retained ancestry and in-flight-write guard. Compaction stamps boundaries but
  does not invoke the retired keep-one deletion flow.
- `services/events_maintenance/daemon.py` no longer schedules checkpoint
  trimming or reports a trim health component.
