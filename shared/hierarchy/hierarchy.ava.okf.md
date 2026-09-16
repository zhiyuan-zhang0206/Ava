---
type: doc
title: Hierarchical Understanding
description: The pristine partition layer of the hierarchical understanding tree — the level-0 block fold over the console item stream plus the append-only seal cascade that cuts narrative nodes (task #3704).
tags:
- hierarchy
- understanding
- timeline
---

# Hierarchical Understanding

## What it is

`shared/hierarchy/` is the pure, deterministic partition layer of the
hierarchical understanding tree. The design contract (user-confirmed
2026-09-14, task #3243): a node is **one text** read by humans and agents
alike; units fold in batches of kappa = [5,15]; sealing is append-only.
This package owns **structure only** — generating node text, storing nodes,
and serving them to the run timeline are the layers built on top.

- `shared/hierarchy/blocks.py` — `fold_blocks(items)` folds the console item
  stream (`shared.timeline.build_timeline_items`) back into level-0 blocks:
  one AI message plus the tool results it answers, or one inbound message.
  Markers are transparent; compact items close the open block (trigger
  points). The fold reproduces the pilot `blocks_and_triggers` partition
  exactly — pinned by the Q7 equivalence evidence (task #3704).
- `shared/hierarchy/seal.py` — `split_units` / `seal_cascade` cut the unit
  stream into sealable groups at trigger points (compact completion;
  day-boundary backstop), level by level. A sub-kappa tail carries to the
  next trigger; a group of one aliases without generating a summary; a
  per-group source guard re-splits oversized groups. `NodeSpec.budget_tok()`
  fixes each node's narrative budget (source/10, hard-capped).

## Invariants

- **Deterministic**: tree = f(unit stream, trigger positions, params) — the
  same input yields an identical tree (structure, ids, spans).
- **Append-only**: sealed nodes never change; only the pending carry moves.
- **Budget**: a node's text must fit `budget_tok()`; the generation pass is
  checked against it mechanically, this layer only fixes the accounting.
