---
type: doc
title: Inspector Panel
description: The right panel's inspector view — per-agent current-state, statistics and widget queries, their caching and refresh rules, and the ContextBreakdown surfaces.
tags:
- frontend
---

# Inspector Panel

current state, window statistics, and plugin widgets have independent open-only queries and loading/error/retry states. Only the selected agent is queried; rows never prefetch on hover or selection. Queries consume abort signals, retain each agent's snapshot for the 30-minute switch window (`lib/switch-budget.ts`), and refetch on selection/manual refresh/60s intervals; a back-switch inside the window renders the retained snapshot immediately (no skeleton) and revalidates only the two live reads (the widget set is selection-invariant). Notice events refresh current state, task events refresh widgets; reconnect repairs those current domains immediately while statistics reconcile on their next interval. Compact invalidation remains commit-safe. Each payload must match the selected agent/window. Matching data survives a refresh failure with an amber marker. Closing aborts HTTP reads; gateway shared historical work remains bounded by admission/deadlines rather than by one cancelled waiter. `["agent-pages"]` refetches on page events. The composer's context-usage button opens **ContextBreakdown** (`context-breakdown.tsx`, #650), whose body (`context-breakdown-body.tsx`) is code-split until expansion while the live meter remains in the initial bundle—an anchored panel expanding upward in-place (not a modal), lazily querying `GET /api/agents/{id}/context-breakdown`, color-coded by category (system_prompt/compact_summary/cluster_memory/agent_memory/context_note/user_input/agent_messages/automation/reasoning/output/tool_call/tool_response; legend rows sorted by context share descending, output labeled "Text output", context_note+automation merged into one "System notes" row). Since task #4023 (P4-3) the same body also renders as a standalone card (**ContextBreakdownCard**) below the run page's chart and window metrics — same query and rendering, thresholds read from the response's mirrored fields (`max_input_tokens`/`soft_compact_tokens`/`hard_compact_tokens`; the composer's live values stay with the collapsed `ContextMeter`).

Parent: [[ui/web/src/docs/frontend-components/frontend-components.ava.okf.md]].
