---
type: doc
title: Model Context Budget
description: 'Explicit catalog window resolution and compaction thresholds.'
tags:
- llm-inference
---

# Model Context Budget

- `resolve_context_budget(model, *, catalog, overrides)` → `ContextBudget(max_context_tokens, soft_compact_tokens, hard_compact_tokens)`: hard = `min(auto_compact_fraction × window, auto_compact_ceiling_tokens)`; soft = `compact_reminder_fraction × window` (scaled down when the ceiling bites). One flat rule for the whole roster — soft 30% / hard 40% of each model's own window (`DEFAULT_TUNING` 0.3/0.4, ceiling 0 = no cap, no per-model compact override), per-agent overridable (the agent passes its slices' `overrides`, which `resolve_setting` takes as the explicit layer; the stream timeouts, reasoning effort and thinking budget resolve the same way); registry entry ⇒ correct thresholds, no parallel table. Unregistered models raise `UnknownModelWindowError` (compact hook bubbles it; gateway display degrades to 0/0/0).
- `latest_input_tokens(messages)` → `input_tokens` from the latest AIMessage with `usage_metadata` (provider's true occupancy), `None` if absent (first turn/post-compact). Shared by compact trigger (`agent/hooks/compact.py`, Option Y) and token-usage endpoint — one unit for gauge/scale/trigger.
