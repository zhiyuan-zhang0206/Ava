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

During streaming each line is memoized `TimelineRow` + `React.memo` PythonCode/ChatMarkdown to suppress unchanged-row renders. Grouping is reused while the items reference is unchanged; nested sticky geometry is measured only inside the selected top-level turn. Sticky bottom auto-scroll + last-item fork.

## Segments and dividers

Historical ranks group separately; localized dividers never enter items or anchor counts — the rank-0 dashed divider labels the live boundary into the current post-compact segment ("Context compacted", task #3698), while the other historical ranks carry the scroll-back label (original history before compact); the rule carries long dashes at a 1:1 ratio and a demoted tone, and a plain label carries no arrow glyph (user feedback 2026-09-17, task #3870). The dividers are pure labels — no load-earlier control exists: reaching the top of the viewport auto-loads the previous page (a small top spinner shows while the fetch is in flight; no button, no pull gesture — task #4186).

## Cross-compact paging

The active view retains the full loaded list: scroll-up paging follows backend `has_more` until the configured `AVA_TIMELINE_COMPACT_HISTORY` depth is exhausted. Loaded history and mounted DOM remain proportional to the history opened; CSS containment does not virtualize either. After a compact the retained-history segments re-attach automatically above the new summary — the store edge and retention hook live in [[ui/web/src/frontend-state/timeline-cache.ava.okf.md|Per-Thread Timeline Cache]].

## Deep collapse

`runs.classifyItem` classifies items as primary (agent replies + human inbound, always visible) / secondary (thinking / code / output, inter-agent messages, system inbound, compact, system_prompt, note marker) / bare (ephemeral marker). Adjacent secondary items form a `TurnBlock`; Details All/Last/None controls its default expansion, with Last opening the active final turn.

## Turn timer

The turn header's timer reads ONE basis in both states — `summarizeTurn.workedMs`, the sum of the turn's block durations — with the live "Working for" adding only the in-flight block's elapsed on top, so it does not drop when the turn ends. Wall-clock across a turn is never displayed: a turn is a maximal run of secondary items and can span an idle gap (a restart, a wake-up the agent had not picked up yet).

## Relationship to Other Nodes

- [[ui/web/src/frontend-components/frontend-components.ava.okf.md|Frontend Components]] — the catalog this node was split out of; the sidebar, composer, and inspector live there.
- [[ui/web/src/frontend-state/timeline-cache.ava.okf.md|Per-Thread Timeline Cache]] — the store that feeds this view, including the compact-replace edge and the retention re-attach.
