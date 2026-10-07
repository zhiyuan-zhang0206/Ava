# The understanding tree is built by hand per session, and the levels above are rebuilt by a queued job

Decision (2026-10-07): what the producers did not cover (an agent that never reached a size cut,
history from before the pipeline) is built on request, one *session* at a time, through the
chunk queue that already exists. This replaces the single-purpose `POST .../understanding/close`.

What is decided:
- **A session is a compaction segment.** Numbered from 1 (oldest), so a number never changes; the
  newest segment, with no boundary checkpoint, is the session in progress. Building it is what
  closing the live segment was.
- **A build is the live trigger rule, replayed, minus coverage.** The chunks are cut as the hook
  would have cut them (`AVA_UNDERSTANDING_CHUNK_TOKENS`, the closing remainder last); a chunk that
  overlaps level-1 nodes is cut down to its uncovered runs, one job each, so a repeated or
  overlapping build never re-describes what exists. The jobs are ordinary chunk jobs.
- **The levels above are rebuilt from the leaves, by a job.** A build also queues one
  `understanding_rebuilds` row per agent (builds merge into the agent's pending row). The consumer
  loop claims it once the agent's chunk jobs have ended, drops every node above level 1 and the
  grouping cursor, and replays the leaves in message order with the normal grouping checks (a replay
  horizon makes each check see only the leaves that had "landed"). It is restartable from the
  lifted state, and the agent's chunk jobs are held back while it runs.
- **A dry run prices before anything is spent.** Cold cache at the full input rate, because a
  manual build reads a stored history, not a warm one; the factors (calls per job, output tokens)
  are measured, the rates are the price book's.

Rejected, and why:
- *A free-floating task in the gateway that waits for the jobs and then rebuilds.* Background work is
  a service loop; a task dies with the gateway and nothing would restart it.
- *Rebuilding from the last chunk job's own task.* Jobs of one agent can be claimed by different
  runners and the last to end is not known to it; a queue row is the one place that both survives a
  restart and merges concurrent builds.
- *A `kind` column on `understanding_chunk_jobs`.* The chunk columns (indices, end message id,
  boundary) mean nothing to a rebuild, and the claim query would grow a second personality.
- *Growing the upper levels as the build's leaves land.* Sessions are described out of order and in
  bulk; a grouping over a level with holes is wrong, and the tree would be redone anyway.
- *A ratio of the model's context window as the chunk size.* The live hook uses the absolute token
  growth; a build must cut where the hook would have.
