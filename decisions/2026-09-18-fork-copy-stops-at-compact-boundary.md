# Fork copies back to the newest compaction boundary, not the whole chain

## Context

Spawning a fork started failing with `psycopg.errors.QueryCanceled: canceling
statement due to statement timeout` in `_copy_checkpoint_chain`
(`ops/agent_spawn.py`). The gateway itself was healthy — 200s at ~0.2s — and
every failure named the same source thread.

The cause is an interaction between two mechanisms that are individually
correct:

- Delta-written threads are *deliberately* exempt from checkpoint trimming (the
  never-delete ruling, tasks #3180/#3181): `_TRIM_SQL`'s `delta_thread` guard
  makes the reaper skip them. Retention is the point — chains are meant to grow
  monotonically and stay recoverable. Measured on the live cluster: 766,183
  checkpoints total, with single threads at 67,032 / 48,639 / 46,034. Chain
  length is therefore a given, not a defect. What must hold is that everything
  reading a chain costs what it actually needs, not what the chain has
  accumulated.
- `_copy_checkpoint_chain` walked `parent_checkpoint_id` all the way to the
  root and copied every ancestor, plus *every* blob row of the source thread
  (`WHERE thread_id = src`, unfiltered — its own docstring conceded that the
  extra blobs go unread).

So fork cost was proportional to a thread's entire lifetime. Past ~1 minute of
copying it exceeds `statement_timeout` and no fork of that agent can succeed
again — a permanent, silently worsening failure.

## Decision

The copy walks up from the fork point and stops at (and including) the first
checkpoint stamped `compact_boundary`. Blobs are narrowed to the
`(checkpoint_ns, channel, version)` triples the copied checkpoints actually
reference through `channel_versions`.

A boundary checkpoint is written by `mark_compact_boundary` as the
full-snapshot record of the segment it closes, so it resumes without any
ancestor of its own. That makes the copy bounded by one compacted segment —
roughly one model context window — instead of by thread age. A thread that
never compacted has no boundary and is still copied to its root, which is that
thread's whole history and by definition under one context window.

The blob filter uses the same join `PostgresSaver.SELECT_SQL` uses to read
blobs (`jsonb_each_text(checkpoint -> 'channel_versions')` joined to
`checkpoint_blobs`), so nothing a reader can reach is left behind.

The property this protects is complexity, not storage. A fork copies one
segment regardless of how long the source chain is, so forking repeatedly over
a thread's life writes O(N) rows instead of O(N²). Nothing is deleted and
nothing is meant to be.

Agent-visible semantics are unchanged, and arguably tightened: after a
compaction the source agent itself only sees post-boundary history, so a fork
now starts from exactly what its parent currently sees.

## Alternatives rejected

- **Reference the parent's rows instead of copying (the "just point at it"
  model).** LangGraph's `PostgresSaver` resolves a chain by recursing on
  `parent_checkpoint_id` *within one `thread_id`*; there is no cross-thread
  parent pointer, and `thread_id` is a framework column. Getting this would
  mean shipping a custom checkpointer that owns chain traversal, and then
  carrying that fork against every upstream schema change. Far larger blast
  radius than the problem warrants.
- **Raise `statement_timeout` for the spawn path.** Treats the symptom. The
  copy stays proportional to thread age, so it only moves the cliff — and it
  moves a growing amount of write amplification into every fork.
- **Make delta threads trimmable so chains stay short.** Rejected on intent,
  not merely on risk: retention is deliberate (#3180, reaffirmed by user ruling
  2026-09-18 — "checkpoints only grow, that is the correct behavior"). Trimming
  history to make a reader cheap trades a complexity bug for data loss. Fork
  must not depend on another subsystem keeping chains short; that coupling is
  what broke here.
- **Copy only the fork-point checkpoint.** Correct only for full-snapshot
  threads. Delta-written threads keep message content in `checkpoint_writes`
  attached to earlier checkpoints of the segment, so the replica would lose
  message history. Stopping at the boundary keeps exactly the segment those
  writes belong to.

## Consequences

- Fork cost is bounded by one compacted segment instead of thread lifetime;
  the failure mode it caused is structural, so this removes a class of spawn
  outage rather than one instance.
- Pre-boundary history is no longer carried into forks. The source thread is
  untouched and still holds it; `get_ancestors` / fork provenance
  (`fork_source_agent_id`, `fork_source_checkpoint_id`) are unaffected.
- Superseded blob versions stay with the source. They were already unreachable
  from the copy — `SELECT_SQL` only resolves versions named in
  `channel_versions`.
- Existing chains are untouched, and stay that way by design: monotonic
  retention is the intended behavior, so a growing store is not a defect to
  chase. The target is a *linear* store, not a small one — bounding each fork
  to one segment is what keeps total write volume linear in a thread's life
  instead of quadratic.
