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
   group's first / last `ava_created_at`, engine `chunk-0.2`, prompt `chunk-0.13`. Upper-level checks follow
   ([[base/agents/history/hierarchy/docs/groups.ava.okf.md|groups]]).

Raw call record (`understanding_chunk_calls`): one row per provider call — job, attempt, round,
model, the full instruction (the correction, for one), `prefix_len` / `start_offset`, reply,
usage, `duration_ms`, `error`, `kind` (`leaf` / `group-correction`), `problem` (why the groups
were refused). A write failure is the `understanding_call_record_failed` event.

Outcomes: a checkpoint not caught up requeues the job; drifted indices fail it
(`understanding_chunk_failed`); a Gemini model (its explicit-cache path strips the head) or an
empty chunk is `skipped`; a generation error (a grouping reply still refused after the
corrections included) retries up to three attempts, then fails; raw records stay.

## Grouping inside the call (`chunk_generate.py`, `leaf_groups.py`)

English instruction after the prefix. It opens with a line setting it apart from the messages
before it (the prefix can end in a framework reminder that would otherwise read as part of the
task), then: divide the part listed in the catalog into consecutive groups, keeping consecutive
units about the same matter together, and summarize each (a node above the raw messages, much
shorter, not a handoff, in the conversation's language); the catalog is the complete ordered
list, referred to by number, not to be matched against the messages above, and the summaries
describe only the listed units; what a unit is; parenthesized lines are framework messages to join
to a neighbour. The **numbered catalog** has one unit per line: `[number] type: content`
(`build_catalog`, no time; `units.catalog_line`, whitespace collapsed). The type is code-made:
inbound the sender (`human message`, `agent N message`, `watcher N`), `agent text`, `work`.
Inbound and text show 100 characters of content; work shows `reasoning[..] | call[..] |
output[..]` (corner brackets), each 60 characters, an empty part left out, the call without import lines; a framework **note** shows a label by type (`units.note_label`: `(memory)`,
`(system note)`, `(compact summary)`, `(attachment)`, a note's tag otherwise). A chunk of only
notes is `skipped` without a call. Nothing else is prescribed. Every unit is in a group. The reply
names each group by the numbers of its first and last unit, like the upper levels:

    <group first="1" last="16">summary of units 1 to 16</group>
    <group first="17" last="30">summary of units 17 to 30</group>

Code looks the numbers up (`leaf_groups.resolve_groups`); no text is matched. Both ends are
written so the numbers can be checked: the groups must tile the catalog (first group at 1, each
starting right after the previous `last`, `last` not before `first`, the last group ending at the
catalog's last unit). A model that numbers its groups 1, 2, 3 instead of naming units cannot end
its last group at the catalog's end, so it is refused and not stored with every span shifted
(agent 9, job 149). Other collected problems: a malformed envelope (a `<group` / `<open` tag that
is not a complete element: a missing `</group>` would merge groups; there is no `<open>`), a
number not in the catalog, an empty summary. A group starting on a turn's tool calls whose text
unit comes right before starts at that unit, silently (spans stay unique). A refusal names the
offending group's first and last catalog lines (40 characters each) and says the numbers are unit
numbers, not group positions. Problems go back in the same conversation (prefix, instruction, its
own reply unchanged: cached) for corrected groups, up to `AVA_UNDERSTANDING_GROUP_CORRECTIONS`
(2); then `GenerateError`.

## Known limits

- Crash repair (`agent/hooks/repair.py`) shifts later indices mid-segment: a job past it fails as drift.
- A closing chunk is cut to its snapshot, which can lag the live state; that tail is undescribed.
- `generation_by_span` keys cost by chunk span: group nodes match no call yet.
