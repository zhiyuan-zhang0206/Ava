---
type: doc
title: Context Window — Context Management
description: Agent context window management — how message history is compressed as it approaches LLM token limits. The core mechanism is compaction.
tags: []
---

# Context Window — Context Management

## What It Is

Agent context window management — how message history is compressed as it approaches LLM token limits. The core mechanism is compaction.

## Core Mechanisms

### Compaction (`ava.self.compact`)
- Agent invokes `ava.self.compact(summary)` to actively compact.
- Replaces the entire message history with a summary.
- The summary must follow a standard format: Requests / Progress / In flight / Dead ends / Pitfalls / Verbatim tail.
- Before compaction, flush persistent state to disk (workspace files, handoff docs)
- Every applied replacement emits telemetry `compaction_completed`: `compactions=1` is the frequency counter, while `history_chars`, `summary_chars`, and `summary_history_ratio` show the size reduction. The history excludes the standing system prompt because it is re-established rather than discarded; an empty history omits the ratio

### Automatic Compact Thresholds (per-model, #617)
- Hard = `min(auto_compact_fraction × MODEL_CONTEXT_WINDOW[model], auto_compact_ceiling_tokens)`; soft = `compact_reminder_fraction × window` (under the ceiling the same ratio compresses, preserving headroom). Per-model layering via `resolve_setting` (base 0.3/0.4, ceiling 0 = no cap), resolved by `shared/lm/context_budget.py:resolve_context_budget`, so a new registry model derives its thresholds automatically.
- **One flat rule across the roster**: soft 30% / hard 40% of each model's own window — no registry entry carries a compact fraction or ceiling, so the absolute thresholds differ per model only through the window (e.g. 60K/80K on a 200K model, 300K/400K on a 1M one). Decision: `decisions/2026-07-31-flat-compact-thresholds.md`; the superseded per-model evidence tiers: `decisions/2026-07-25-per-model-tuning-values.md`.
- **Why a ceiling knob at all**: windows grew ~8× (128K→1M) while effective context didn't, so one fraction means a different absolute budget per model; the ceiling is the escape hatch for pinning an absolute trigger (per-model in the registry, or cluster-wide via `AVA_AUTO_COMPACT_CEILING_TOKENS`). Currently unused — 0 everywhere.
- Unregistered models fail fast with `UnknownModelWindowError`; gateway display endpoints catch it and degrade to 0/0/0
- **Trigger occupancy unit**: the last LLM call's real `input_tokens` (chars/4 before the first turn) — gauge, ticks, and trigger share one unit, read through the shared `auto_compact_will_fire` predicate.
- **Display surface**: `/api/agents/{id}/token-usage` carries the resolved thresholds (ContextMeter ticks); `/api/agents/{id}/context-breakdown` (`gateway/context_breakdown.py`) buckets messages by kind and splits the system prompt by `#` section, normalized so the categories sum to the provider's value.

## Key Dependencies

- [[context-notes.ava.okf.md]] — the standing head the compaction re-establishes
- [[system-prompt.ava.okf.md]] — the system prompt is the most stable part of the context
- [[agent/graph/graph.ava.okf.md]] — `init_context` is a graph node ahead of claim; `_memory_recall.py` fires as a before_llm hook
- [[shared/lm/lm.ava.okf.md]] — `MODEL_CONTEXT_WINDOW` + `context_budget.py` are the single source of truth for soft/hard thresholds
- [[routers.ava.okf.md]] — token-usage / context-breakdown display endpoints

## Entry Points
- `ava/self.py:compact(summary)` — active compaction entry
- `agent/hooks/compact.py` — auto-compact trigger hook (Option Y occupancy determination)
- `shared/lm/context_budget.py:resolve_context_budget()` — per-model soft/hard threshold resolution
