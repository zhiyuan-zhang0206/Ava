---
type: doc
title: Model Context Budget
description: 'Explicit catalog window resolution and compaction thresholds.'
tags:
- llm-inference
---

# Model Context Budget

- `resolve_context_budget(model, *, catalog, overrides)` → `ContextBudget(max_context_tokens, soft_compact_tokens, hard_compact_tokens)`: hard = `min(auto_compact_fraction × window, auto_compact_ceiling_tokens)`; soft = `compact_reminder_fraction × window` (scaled down when the ceiling bites). The shared floor is soft 30% / hard 40% of each model's own window (`DEFAULT_TUNING` 0.3/0.4, ceiling 0 = no cap). The `deepseek-flash` declaration pins 0.374/0.512 on its 1M window (374k/512k), retaining the 2026-08-29 user decision, per-agent overridable (the agent passes its slices' `overrides`, which `resolve_setting` takes as the explicit layer; the stream timeouts, reasoning effort and thinking budget resolve the same way); registry entry ⇒ correct thresholds, no parallel table. Unregistered models raise `UnknownModelWindowError` (compact hook bubbles it; gateway display degrades to 0/0/0).
- `latest_input_tokens(messages)` → `input_tokens` from the latest AIMessage with `usage_metadata` (provider's true occupancy), `None` if absent (first turn/post-compact). Shared by compact trigger (`agent/graph/hooks/compact.py`, Option Y) and token-usage endpoint — one unit for gauge/scale/trigger.

Regression assertions: [test contracts](../../../packages/tests/lm/docs/contracts.ava.okf.md#scaled-budget).
