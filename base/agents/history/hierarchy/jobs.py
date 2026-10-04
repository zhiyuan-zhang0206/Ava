"""The `hierarchy_jobs` row vocabulary shared by every writer and reader.

One row is one build attempt of one agent (task #3704 P2b). Rows end in
`done` or `failed`; two of those endings are bookkeeping rather than attempt
outcomes, and every reader that paces retries must know them:

- the silent baseline (`scan.SILENT_BASELINE_MARKER`): a `done` row that
  retired pre-existing history — never a build;
- the orphan reclaim (`ORPHAN_MARKER`, task #4975): a `running` row whose
  holder process is gone, flipped to `failed` by the worker's holder reap
  (`services.hierarchy_worker.runner.reap_orphans`) or by the enqueue's
  conflict supersede (`base.agents.history.checkpoint_cleanup`) — never an
  attempt outcome, so the pacing reads skip it entirely: an interrupted run
  must neither wait out the retry backoff nor count toward the failure
  streak.

A `running` row is reclaimable as an orphan when no live holder can still be
serving it. Two proofs exist, each reachable from where it is applied:

- the worker, on the gateway host where every job child runs: the row's
  holder process is gone — the pid is dead, or the child died before it
  registered (`execute.execute_job` stamps its own pid at boot). `ava stop`
  kills processes, so this is the stop-left row; a crash that left the child
  alive is deliberately left alone;
- from any process, at any time: the row has outlived the job deadline plus
  the stale grace — the same window the scan's stale sweep uses, because a
  live parent kills a wedged child at the deadline and recovers its row. The
  enqueue applies that proof at the moment of its conflict (a remote process
  cannot read the holder's pid table); the window itself is never shortened.

The marker is matched exactly (`error IS DISTINCT FROM ORPHAN_MARKER`), so
the text is a module constant both sides import.
"""

from __future__ import annotations

from typing import Final

# The `error` text every orphan reclaim writes; a `failed` row carrying it is
# paced through (see the module docstring), never counted as an attempt.
ORPHAN_MARKER: Final[str] = "orphan reclaimed: holder process gone (task #4975)"
