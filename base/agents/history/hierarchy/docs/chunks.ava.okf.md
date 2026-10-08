---
type: doc
title: Chunk-triggered Understanding
description: Understanding by context chunk — a provider-reported token threshold cuts each compaction segment, a queue carries the chunks, the agent host's loop has the agent's own model group each chunk's message units and summarize every group into depth-1 nodes.
tags:
- hierarchy
- understanding
- chunks
- queue
---

# Chunk-triggered Understanding

The producer of the understanding tree's level 1. It
describes an agent's context **while it grows**, in chunks, and writes level 1: depth-1 rows,
one per **group of layer-0 message units** (`units.py`: inbound, text, reasoning + tool call +
result) the model splits a chunk into by meaning. The levels above:
[[base/agents/history/hierarchy/docs/groups.ava.okf.md|Upper-level Grouping]].

Off by default (`AVA_UNDERSTANDING_ENABLED`). `AVA_UNDERSTANDING_CHUNK_RATIO` (default 0.5, in (0, 1]) sets the chunk size: ratio x the agent model's soft compaction threshold (`chunk_threshold`, with agent overrides), ~187K on DeepSeek; shared by hook, build, replay.

## Trigger (`agent/hooks/understanding_chunks.py`, `agent/graph/llm/node.py`)

After every llm turn the node compares the provider-reported `input_tokens` of the request
with the tokens at the segment's previous cut (`CompactState.understanding_cut_index` /
`_tokens`). At the threshold the chunk `[cut index, request length)` is enqueued and the cut
moves to the request's end. Indices are positions in the request list, SystemMessage head at 0.
A segment's **head** is its leading run of framework-injected messages (`segment_head_len`); the
first chunk starts after it. A failed enqueue leaves the cut; `next_segment()` bumps `version`,
zeroing it.

**Compaction closes the segment**: `stamp_compact_boundary` enqueues the remainder from the
last cut to the segment's end, with the boundary checkpoint id it stamped.

**Manual build per session**: `GET /api/agents/{id}/sessions` and
`POST /api/agents/{id}/understanding/build` (`gateway/agents/history/understanding.py`;
`sessions.py`, `build.py`, `chunk_plan.py`). A *session* is one compaction segment (1 = oldest, stable; the unclosed newest has no boundary). `build.plan_jobs` replays the live
trigger rule over the chosen sessions (`chunk_plan.plan_replay`, threshold
`chunk_threshold`, as the hook), cuts every chunk down to the runs no level-1 node covers (one job per
run; a run of framework notes only is dropped) and enqueues ordinary chunk jobs (a closed session's
name its boundary checkpoint; an ended job of the same stretch is revived, a live one merged into).
The session in progress is built to the last request sent, which is what closing the live segment
was. `dry_run` plans and prices only. Estimates are cold-cache (`build.COST_BASIS`). A build
is recorded in `understanding_builds` (its jobs, its rebuild) and read back by
`GET .../understanding/builds/{build_id}`.

**Rebuild of the upper levels** (`rebuild.py`, table `understanding_rebuilds`): a build also queues
one rebuild for its agent (builds merge into the agent's pending row under an advisory lock). The consumer loop claims it only when the agent has no chunk
job pending or running; while it runs, the agent's chunk jobs are not claimed. It drops every node
above level 1 and the agent's `understanding_group_state`, then replays the level-1 nodes in
message order, running the grouping checks after each with that leaf as the horizon
(`run_group_checks(upto=...)`), as live leaves grow it. An interrupted rebuild
restarts from the lifted state; chunk jobs finishing while one is pending skip their grouping checks.

## Queue (`chunks.py`, table `understanding_chunk_jobs`)

One row per `(agent_id, compact_version, start_index, end_index)` (re-enqueue is a no-op), with
`end_msg_id` and, for a compaction's closing chunk, `boundary_checkpoint_id`. Enqueue is
best-effort: a failure is the `understanding_enqueue_failed` event and never touches the turn.
Claim is `FOR UPDATE SKIP LOCKED` over pending rows (a retry after a spacing) and `running` rows
whose lease lapsed, only the oldest live job of its agent: runners share the queue, a crashed
claimer's row is taken over. Statuses: pending, running, done, failed, skipped.

## Consumer (`chunk_consumer.py`)

A loop of the agent host (`services/agent_runner/agent_host/daemon.py`, given the agent's
`execute_code` schema), quiesce-gated, never raising. Every due job runs at
once, with no cap ([[base/agents/history/hierarchy/docs/concurrency.ava.okf.md|concurrency]]). Each poll
samples the queue as `understanding_backlog`. A job, claimed:

1. is described on its own: a chunk never depends on the previous one, and a topic that crosses
   its end is two nodes (the upper levels join them);
2. locates the chunk in the stitched history (`locate_chunk`): a live chunk by `end_msg_id`
   at its recorded index; a closing chunk in its boundary checkpoint's segment;
3. builds the agent's own model (`agent_model_target`, `ModelCache`) and sends `[segment head,
   messages[:end], instruction]` with `execute_code` bound; a tool-call reply is refused and
   retried (`generate._invoke_agent_shaped`);
4. writes a depth-1 row per group (`write_group_nodes`, one transaction): span in stitched
   indices (first unit's first message to last unit's last), `start_ts` / `end_ts` from the
   group's first / last `ava_created_at`, engine `chunk-0.2`, prompt `chunk-0.13`, and the `job_id` of the job. Upper-level checks follow
   ([[base/agents/history/hierarchy/docs/groups.ava.okf.md|groups]]).

Raw call record (`understanding_chunk_calls`): one row per provider call — job, attempt, round,
model, the full instruction (the correction, for one), `prefix_len` / `start_offset`, reply,
usage, `duration_ms`, `error`, `kind` (`leaf` / `group-correction`), `problem` (why the groups
were refused). A write failure is the `understanding_call_record_failed` event.

Outcomes: a wait (checkpoint not caught up, a database blink) requeues the job, no attempt spent,
the spacing counted from the requeue; the give-up clock (`waiting_since`, 6 hours) starts at the
first wait, so a job queued while the feature is off is not timed. Drifted indices (`end_msg_id`
not where recorded, a closing chunk's too) fail it (`understanding_chunk_failed`). A Gemini model
(its cache path strips the head), an empty chunk or one already covered is `skipped`. A chunk
overlapping existing level-1 nodes is cut to its first uncovered run (`covered_spans`,
`uncovered`) and the other runs go in `understanding_chunk_gap`: nodes of a level never overlap
and nothing is dropped silently. A closing chunk past its boundary snapshot is cut to what the
snapshot holds, written, and the missing request indices are in the same event. A generation error
(a reply still refused after the corrections included) retries up to three attempts, then fails;
raw records stay. A host stopping puts its running jobs back at once (no attempt); a crash relies
on the 60-minute lease (not renewed: longer than any honest job).

## Grouping inside the call

The instruction, the numbered catalog and the reply envelope (`<group first last>`, which must tile the
catalog) are [[base/agents/history/hierarchy/docs/chunk-grouping.ava.okf.md|Chunk Grouping]].

## Known limits

- Crash repair (`agent/hooks/repair.py`) shifts later indices mid-segment: a job past it fails as drift.
- A compaction stamps its boundary after waiting up to 5 s for the newest checkpoint to be as
  new as the read time of the state's last message (`await_snapshot`, one cheap read per
  poll; the graph persists a super-step asynchronously, so the newest row can lag; a last message
  with no usable time, none or no timezone, takes the timeout path at once). On timeout it stamps anyway and emits `understanding_snapshot_lag`; the
  closing chunk then covers what the snapshot holds and reports the rest as a gap.
- A node's generation cost is its job's (or check's) cost: all the groups of one job share it.
