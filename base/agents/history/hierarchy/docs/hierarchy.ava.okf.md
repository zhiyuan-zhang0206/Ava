---
type: doc
title: Hierarchical Understanding
description: The understanding tree of an agent's history — layer-0 message units, chunk calls that group and summarize them, upper-level grouping, and the reads that serve the run-timeline.
tags:
- hierarchy
- understanding
- timeline
---

# Hierarchical Understanding

## What it is

`base/agents/history/hierarchy/` turns one agent's message history into a tree of summaries for
audit: what happened, drillable level by level down to the raw messages. A node is one text; the
tree is stored in `understanding_nodes`, keyed by the message span `(agent_id, depth, span_start,
span_end)`. Compaction serves the agent going on working and may forget; this tree serves
auditing and aims to be faithful, so the two do not shape each other.

- **Layer 0** (`units.py`): the deterministic message units — a work unit is reasoning + tool
  call + its results, agent text and each inbound message are units of their own, framework notes
  are units. No LLM. `read_times` gives each message the time the model read it (running maximum of
  the message read times, identity for data that records the pickup); `display_blocks` splits a
  work unit into thinking / call / output blocks for the run-timeline only, on those times.
- **Level 1** ([[base/agents/history/hierarchy/docs/chunks.ava.okf.md|chunk calls]]): the agent's
  own request prefix plus one instruction, so the provider serves the prefix from cache; the model
  groups the numbered catalog of units and summarizes each group. `chunks.py` (queue, trigger,
  nodes), `chunk_consumer.py` (the agent-host loop), `chunk_generate.py` (instruction, catalog,
  reply), `leaf_groups.py` (reply parsing).
- **Levels above** ([[base/agents/history/hierarchy/docs/groups.ava.okf.md|upper-level grouping]]):
  a level's open nodes are grouped by a plain-text call with no agent prefix; `group.py` (prompt,
  reply checks), `group_consumer.py` (when a level is due), `group_store.py` (writes).
- **Reads** (`store.py`, `serve.py`, `usage.py`): the nodes with their deterministic costs for
  the run-timeline ([[services/derived/insights/run_timeline/docs/run_timeline.ava.okf.md|run timeline]]).
- **Provider calls** (`generate.py`): the model built the way the agent builds its own, the
  agent-shaped request with its bounded refusal of tool calls, and the raw record of each call.

How much runs at once, and how rate limits are met:
[[base/agents/history/hierarchy/docs/concurrency.ava.okf.md|consumer concurrency]].

## Invariants

- **Deterministic structure**: layer 0 and every cost figure are code; only "what happened" is
  an LLM text.
- **Audited**: every provider call is persisted whole (`understanding_chunk_calls`,
  `understanding_group_calls`), failures included.
- **Stable spans**: compaction boundaries are never trimmed (#1125), so the stitched full
  history is append-only and a span's identity never shifts; a span outside the history is an
  explicit error.
- **No gap hidden**: a chunk that fails for good stays undescribed and emits
  `understanding_chunk_failed`; the timeline shows the raw messages there.
