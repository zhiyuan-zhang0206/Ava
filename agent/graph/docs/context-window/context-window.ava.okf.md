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

### Soft preparation and its effectiveness

At the soft threshold, the framework asks the agent to wind down now, persist
new durable information and call `ava.self.compact(summary)` before further
substantive work. Core guidance includes updating the `to_user.md`
document when workspace guidance is enabled. Enabled plugins may add short,
side-effect-free `compact_reminder_sections` through their runtime faces;
core joins them into the same note, with plugin attribution. Peer inbounds
do not defer the reminder: the hook runner's message reducer composes it with
other notes. Delivery remains once per window, keyed by the compact version.

The operating target is at least 95% agent-authored compactions **after a soft
reminder**, among agent-authored plus automatic hard compactions. This is a measured target,
not a claim established by deterministic tests. The soft/hard thresholds
retain their existing gap; the hard ceiling is not raised to improve the
metric. Manual compact requests are outside this denominator.

`compaction_completed` is persisted and includes `reminder_shown` for the
window being replaced, never a stale bookmark from the preceding window.
The existing `compact_kind` distinguishes `compact_summary` (agent-authored),
`auto` (hard fallback), and `compact_request` (manual request). The completed
compactions panel separates agent-authored after a reminder, proactive
agent-authored, hard after a reminder, and hard without a reminder. The soft
share panel shows both all agent-authored and reminded agent-authored ratios;
proactive compaction alone cannot meet the reminder coverage target. It uses only events carrying the new attribution field, shows 0%
when all observed completions are hard, and no data when none completed.

A high hard-after-reminder share calls for reviewing instruction following
and preparation cost; a high hard-without-reminder share calls for examining
large context jumps and configured thresholds. These measurements establish
which path completed, not whether an agent actually saved useful files.
Critical state still needs saving when it changes. Automatic summarization
has no ordinary file-writing turn, so reminders are not appended there.

### Automatic Compact Thresholds (per-model, #617)
- Hard = `min(auto_compact_fraction × ModelSpec.context_window, auto_compact_ceiling_tokens)`; soft = `compact_reminder_fraction × window` (under the ceiling the same ratio compresses, preserving headroom). Per-model layering via `resolve_setting` (base 0.3/0.4, ceiling 0 = no cap), resolved by `base/lm/context_budget.py:resolve_context_budget` from the agent's model and its `overrides` slice, so a new registry model derives its thresholds automatically.
- **Default rule**: soft 30% / hard 40% of each model's own window (e.g. 60K/80K on a 200K model, 300K/400K on a 1M one). Provider tuning and agent overrides may change these values; DeepSeek Flash currently declares soft 374K / hard 512K on its 1M window. Decision: `docs/decisions/agents/context/2026-07-31-flat-compact-thresholds.md`; the superseded per-model evidence tiers: `docs/decisions/engineering/design/simplification/2026-07-25-per-model-tuning-values.md`.
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
- `ava/self/__init__.py:compact(summary)` — active compaction entry
- `agent/graph/hooks/compact.py` — auto-compact trigger hook (Option Y occupancy determination)
- `base/lm/context_budget.py:resolve_context_budget()` — per-model soft/hard threshold resolution


## Bound contracts

- [[preparation.ava.okf.md]] — reminder delivery, reply opportunity and completion attribution.
- [[replacement.ava.okf.md]] — request prefix, clean replacement, failure and checkpoint retention.
