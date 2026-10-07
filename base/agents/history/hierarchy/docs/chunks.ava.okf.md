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

Off by default (`AVA_UNDERSTANDING_ENABLED`); `AVA_UNDERSTANDING_CHUNK_TOKENS` is the chunk size (60000).

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

**Manual close**: `POST /api/agents/{id}/understanding/close` (`gateway/agents/understanding.py`)
enqueues the live segment's undescribed tail (from the last job's end or a failed job's start to
the last sendable request), read from the live checkpoint. Statuses `enqueued` / `empty` /
`active_job`; the gateway reads no `agent` settings, so with the feature off the job waits.
Nothing calls it automatically; its prefix is usually cold (full input price).

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
the spacing counted from the requeue, and fails it after 6 hours; drifted indices (`end_msg_id`
not where recorded, for a closing chunk too) and a closing chunk past its boundary snapshot (its
last turns are in no checkpoint) fail it (`understanding_chunk_failed`); a Gemini model (its
cache path strips the head), an empty chunk or one already covered by nodes is `skipped` (a
partly covered one is shortened to the undescribed part, so level-1 nodes never overlap); a
generation error (a reply still refused after the corrections included) retries up to three
attempts, then fails; raw records stay. The claim lease is 60 minutes, not renewed (longer than
any honest job; a takeover of a live one costs a second run, a crash only the wait).

## Grouping inside the call

The instruction, the numbered catalog and the reply envelope (`<group first last>`, which must tile the
catalog) are [[base/agents/history/hierarchy/docs/chunk-grouping.ava.okf.md|Chunk Grouping]].

## Known limits

- Crash repair (`agent/hooks/repair.py`) shifts later indices mid-segment: a job past it fails as drift.
- A closing chunk's snapshot may lack the segment's last turns (the checkpoint of the previous
  super-step may still be in flight when compaction stamps the boundary; not reproduced): the job
  fails with the event, the gap is visible, and those turns are in no stitched history either.
- A node's generation cost is its job's (or check's) cost: all the groups of one job share it.
