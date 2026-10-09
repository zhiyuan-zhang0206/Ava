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
- The SDK docstring owns this contract. Forced compaction appends it to the request because the model cannot call tools there; the standing P95 SDK reference need not expand `self`. Voluntary compaction can load it through `ava.help(ava.self.compact)`.
- Before compaction, flush persistent state to disk (workspace files, handoff docs)
- Every applied replacement emits telemetry `compaction_completed`: `compactions=1` is the frequency counter, while `history_chars`, `summary_chars`, and `summary_history_ratio` show the size reduction. The history excludes the standing system prompt because it is re-established rather than discarded; an empty history omits the ratio

### Automatic Compact Thresholds (per-model, #617)
- Hard = `min(auto_compact_fraction × ModelSpec.context_window, auto_compact_ceiling_tokens)`; soft = `compact_reminder_fraction × window` (under the ceiling the same ratio compresses, preserving headroom). Per-model layering via `resolve_setting` (base 0.3/0.4, ceiling 0 = no cap), resolved by `base/lm/context_budget.py:resolve_context_budget` from the agent's model and its `overrides` slice, so a new registry model derives its thresholds automatically.
- **One flat rule across the roster**: soft 30% / hard 40% of each model's own window — no registry entry carries a compact fraction or ceiling, so the absolute thresholds differ per model only through the window (e.g. 60K/80K on a 200K model, 300K/400K on a 1M one). Decision: `docs/decisions/agents/context/2026-07-31-flat-compact-thresholds.md`; the superseded per-model evidence tiers: `docs/decisions/engineering/design/simplification/2026-07-25-per-model-tuning-values.md`.
- **Why a ceiling knob at all**: windows grew ~8× (128K→1M) while effective context didn't, so one fraction means a different absolute budget per model; the ceiling is the escape hatch for pinning an absolute trigger (per-model in the registry, or cluster-wide via `AVA_AUTO_COMPACT_CEILING_TOKENS`). Currently unused — 0 everywhere.
- Unregistered models fail fast with `UnknownModelWindowError`; gateway display endpoints catch it and degrade to 0/0/0
- **Trigger occupancy unit**: the last LLM call's real `input_tokens` (chars/4 before the first turn) — gauge, ticks, and trigger share one unit, read through the shared `auto_compact_will_fire` predicate.
- **Display surface**: `/api/agents/{id}/token-usage` carries the resolved thresholds (ContextMeter ticks); `/api/agents/{id}/context-breakdown` (`base/agents/history/context_breakdown.py`) sums each message's own token count (`base/agents/history/message_tokens.py`, anchored to the provider's `input_tokens`) into kind buckets and splits only the inside of a message (an AIMessage's parts, the system prompt by `#` section) with the estimator; every category says whether any part was estimated.

## Key Dependencies

- [[agent/graph/docs/context-notes/context-notes.ava.okf.md]] — the standing head the compaction re-establishes
- [[system-prompt.ava.okf.md]] — the system prompt is the most stable part of the context
- [[agent/graph/docs/graph.ava.okf.md]] — `init_context` is a graph node ahead of claim; `memory_recall.py` fires as a before_llm hook
- [[base/lm/docs/lm/lm.ava.okf.md]] — `ModelSpec.context_window` + `context_budget.py` are the single source of truth for soft/hard thresholds
- [[routers.ava.okf.md]] — token-usage / context-breakdown display endpoints

## Entry Points
- `ava/self.py:compact(summary)` — active compaction entry
- `agent/hooks/compact.py` — auto-compact trigger hook (Option Y occupancy determination)
- `base/lm/context_budget.py:resolve_context_budget()` — per-model soft/hard threshold resolution
