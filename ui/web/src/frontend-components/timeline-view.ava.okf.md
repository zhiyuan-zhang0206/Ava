---
type: doc
title: Timeline View
description: The conversation timeline renderer (`components/timeline/`) — item kinds, memoized streaming rows, segment dividers, cross-compact scroll-up paging, and deep collapse.
tags:
- frontend
---

# Timeline View

`components/timeline/` renders the BackendTimelineItem list (chat / code / output / reasoning / system marker) and is the only item-list surface of a thread. Directory: `index`, `card`, `item`, `buttons`, `markers`, `timestamp`, `reasoning-clock`, `runs` (categorization / grouping), `run-block`.

## Streaming rows

During streaming each line is memoized `TimelineRow` + `React.memo` PythonCode/ChatMarkdown to suppress re-renders at 10-50/s chunk rates. Sticky bottom auto-scroll + last-item fork.

## Segments and dividers

Historical ranks group separately; localized presentation-only dividers never enter items or anchor counts — the rank-0 dashed divider labels the live boundary into the current post-compact segment ("Context compacted", task #3698), while historical ranks keep the scroll-back label (original history before compact).

## Cross-compact paging

Every active and parked-thread item writer retains the full loaded list (no per-thread item cap, task #1734 — scroll-up paging follows the backend `has_more` until the configured `AVA_TIMELINE_COMPACT_HISTORY` depth is exhausted); at most 32 parked buckets are kept. After a compact the retained-history segments re-attach automatically above the new summary — the store edge and retention hook live in [[ui/web/src/frontend-state/timeline-cache.ava.okf.md|Per-Thread Timeline Cache]].

## Deep collapse

`runs.classifyItem` classifies items as primary (agent replies + human inbound, always visible) / secondary (thinking / code / output, inter-agent messages, system inbound, compact, system_prompt, note marker; collapsed by default via `card.messageCardConfig` `fixedDefault=false`) / bare (ephemeral marker); adjacent secondary items are aggregated by `groupIntoTurns` into a `TurnBlock` (collapsed by default, individually collapsible when expanded). Toggle `display.collapse_agent_runs` (default on).

## Turn timer

When `turnActive`, the last item (current streaming step) is peeled out and kept visible. The turn header's timer reads ONE basis in both states — `summarizeTurn.workedMs`, the sum of the turn's block durations — with the live "Working for" adding only the in-flight block's elapsed on top, so it does not drop when the turn ends. Wall-clock across a turn is never displayed: a turn is a maximal run of secondary items and can span an idle gap (a restart, a wake-up the agent had not picked up yet).

## Relationship to Other Nodes

- [[ui/web/src/frontend-components/frontend-components.ava.okf.md|Frontend Components]] — the catalog this node was split out of; the sidebar, composer, and inspector live there.
- [[ui/web/src/frontend-state/timeline-cache.ava.okf.md|Per-Thread Timeline Cache]] — the store that feeds this view, including the compact-replace edge and the retention re-attach.
