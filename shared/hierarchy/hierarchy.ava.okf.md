---
type: doc
title: Hierarchical Understanding
description: The understanding engine — level-0 block fold, append-only seal cascade, deterministic rendering, budgeted node-text generation, and the assembly pipeline (task #3704).
tags:
- hierarchy
- understanding
- timeline
---

# Hierarchical Understanding

## What it is

`shared/hierarchy/` turns one agent's message history into a level tree of
summaries. The design contract (user-confirmed 2026-09-14, task #3243): a node
is **one text** read by humans and agents alike; units fold in batches of
kappa = [5,15]; sealing is append-only. Storage and the run-timeline serving
merge are the layers built on top.

- `blocks.py` — `fold_blocks(items)` folds the console item stream
  (`shared.timeline.build_timeline_items`) into level-0 blocks: one AI message
  plus the tool results it answers, or one inbound message. Markers are
  transparent; compact items close the open block (trigger points). The fold
  reproduces the pilot `blocks_and_triggers` partition exactly — pinned by the
  Q7 equivalence evidence (task #3704).
- `seal.py` — `split_units` / `seal_cascade` cut the unit stream into sealable
  groups at trigger points (compact completion; day-boundary backstop), level
  by level. A sub-kappa tail carries to the next trigger; a group of one
  aliases without generating a summary; a per-group source guard re-splits
  oversized groups. `narrative_budget_tok()` fixes each node's budget
  (source/10, hard-capped).
- `tokens.py` — the one token caliber (o200k) every budget and ratio uses;
  deliberately not a per-model context measure.
- `render.py` — messages to text: deterministic projection per message
  (thinking / text / tool calls / exit codes / inbound texts; ambient context
  skipped) assembled per block, with head+tail truncation for oversized bodies.
- `generate.py` — node-text generation: prompt with the node's character ask,
  bounded parallel fan-out with per-node isolation, over-budget compression,
  `input_hash`/`text_hash` identity helpers.
- `pipeline.py` — assembly: items to blocks to units to trigger batches to the
  seal cascade, then `materialize` walks levels bottom-up (leaves render
  blocks, upper nodes reduce children texts, aliases copy their child) using
  the `known_texts` reuse cache so a rerun over unchanged history costs zero
  calls.

## Invariants

- **Deterministic**: tree = f(unit stream, trigger positions, params) — the
  same input yields an identical tree (structure, ids, spans); rendering is
  pure, so identical input always produces identical generation requests.
- **Append-only**: sealed nodes never change; only the pending carry moves.
- **Budget**: a node's text must fit its budget; over-budget responses get
  bounded compression and a still-over node fails instead of being written.
- **Reuse by content**: `input_hash` covers the engine and prompt versions, so
  a template bump invalidates every cached text rather than silently reusing.
