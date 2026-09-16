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
- `store.py` — persistence on the `understanding_nodes` table (one row per
  `(agent_id, depth, span)` identity): `write_tree` upserts and links parents
  from children spans, `load_known_texts` is the reuse cache read side,
  `load_window_nodes` feeds the run-timeline serving merge.

## Invariants

- **Deterministic**: tree = f(unit stream, trigger positions, params) — the
  same input yields an identical tree (structure, ids, spans); rendering is
  pure, so identical input always produces identical generation requests.
- **Append-only**: compact-sealed cells never change across rebuilds — a
  rebuild reproduces them byte-identically and never deletes them. The
  provisional tail re-cuts as history grows; its superseded rows are
  reconciled away (`store.write_tree`) so storage always mirrors the
  latest partition, while rows a compact-driven pass left pending stay.
- **Budget**: a node's text must fit its budget; over-budget responses get
  bounded compression and a still-over node fails instead of being written.
- **Reuse by content**: `input_hash` covers the engine and prompt versions, so
  a template bump invalidates every cached text rather than silently reusing.
- **Stable spans**: compaction boundaries are never trimmed (#1125), so the
  stitched full history is append-only and the span identity never shifts.
- **Continuable**: a run cut by its deadline reports `skipped` and a
  continuation resumes from the reuse cache — no node is ever redone — and
  the scan cursor (`hierarchy_worker_state.last_processed_boundary`) moves
  only after a run that skipped nothing.

## The worker (task #3704 P2b)

`services/hierarchy_worker/` is the compact-driven builder. It runs as a
gateway-hosted built-in schedule (`schedules/hierarchy-worker-schedule.py`,
built-in `hierarchy-worker`, product class, enabled by default): one cron
slot a minute calls a tick, which scans for new compaction boundaries and
runs one build job at a time — each in its own child process — tracking work
in `hierarchy_jobs` + `hierarchy_worker_state`.

- **Triggers**: one aggregated scan reads every thread's newest compact
  boundary; a boundary newer than the agent's covered cursor enqueues a job
  (idempotent: at most one live job per agent, enforced by a partial unique
  index and an atomic claim). An agent's first sight records the boundary
  silently — no build for pre-existing history; the worker only follows new
  compactions (backfill is on-demand, P2c).
- **The first build is the full retention window** (one-time and bounded),
  with the trigger-time tail sealed — the same semantics as the manual first
  run (`scripts/build_hierarchy_once.py`). Every later compact-driven pass
  seals no tail and leaves it pending for the next compact.
- **Slicing and the zero-redo invariant**: a job self-limits at
  `hierarchy_job_budget_seconds`, newest stretches first; the unattempted
  remainder is recorded as `skipped` and the continuation run replays the
  (pure, cheap) seal cascade, skips everything already materialized via the
  hash cache, and continues there.
- **Failure handling**: node failures ride the job row's scope stats and are
  retried by the next run; a crashed or killed job is recovered by the parent
  process or the stale-running sweep, and non-clean retries back off
  exponentially (base/cap configurable). A budget-truncated continuation
  drains immediately.
- **Knobs** (`settings.daemon.hierarchy_*`, each with its written reason):
  job budget, hard deadline, retry base/cap, generation concurrency, and the
  child-kill / stale-row graces; the generation model is
  `settings.lm.hierarchy_model`.
- **Cost observability**: each job row records the run's scope (stretches,
  nodes generated/reused/failed/skipped) and its token sums; the LLM usage
  ledger (`usage_source='hierarchy.generate'`) is the authoritative per-call
  record.
