---
type: doc
title: Timeline View
description: The conversation timeline renderer (`components/timeline/`) — item kinds, memoized streaming rows, segment dividers, cross-compact scroll-up paging, and deep collapse.
tags:
- frontend
---

# Timeline View

`components/timeline/` renders the BackendTimelineItem list (chat / code / output / reasoning / system marker) and is the only item-list surface of a thread. Directory: `index`, `segments` (compact grouping), `use-compact-transition-anchor` (reading position), `use-timeline-window` (bounded DOM), `card`, `item`, `buttons`, `markers`, `timestamp`, `reasoning-clock`, `runs` (turn grouping), `run-block`.

## Streaming rows

During streaming each line is memoized `TimelineRow` + `React.memo` PythonCode/ChatMarkdown to suppress unchanged-row renders; `TimelineView` itself is memoized against unrelated page renders, and `TurnBlock` compares its rebuilt member ids and summary by value so collapsed historical turns skip each regroup. Every live clock (block chips, the turn header, the compacting block) shares one 100ms ticker in `reasoning-clock`; the interval exists only while a clock is live, and block clocks tick only while the agent is busy (`LiveClockGate`). The load-older overlay animates its spinner only while loading. Grouping is reused while the items reference is unchanged; nested sticky geometry is measured only inside the selected top-level turn. Sticky bottom auto-scroll + last-item fork.

## Pinned headers

Message, work-block, and nested detail headers share one frosted-glass pane.
It keeps the 95% background tint, 30% accent tint on hover, and backdrop blur.
The pane spans the header and its paint-only 2px top overlap so the title-bar/
parent-header seam receives the same blur and tint. The button itself stays
transparent to avoid doubling the tint. The right shadow covers the scrollbar
gutter and the 1px separator sits inside the bottom edge. Pinning changes no
box dimensions or nested-header offsets.

Event timestamps remain visible in message-card headers at narrow widths;
headers and timestamp text wrap when needed. Standing context notes continue
to omit timestamps according to their timeline item metadata.

## Segments and dividers

Historical ranks group separately; localized dividers never enter items or anchor counts — the rank-0 dashed divider labels the live boundary into the current post-compact segment ("Context compacted", task #3698), while the other historical ranks carry the scroll-back label (original history before compact); the rule carries long dashes at a 1:1 ratio and a demoted tone, and a plain label carries no arrow glyph (user feedback 2026-09-17, task #3870). The dividers are pure labels — no load-earlier control exists (paging is driven by reaching the top; task #4186).

During a compact transition, the view renders buffered old groups above the
new rank-0 window with provisional historical ranks and a dashed live boundary.
Buffered and canonical React keys occupy separate namespaces; canonical history
already covered by the buffer stays hidden until a single store update replaces
the buffer with the re-keyed rows. A pre-commit notification captures the topmost visible
real row before that transition commit, and a layout effect maps its message/block
coordinate to the new rank and adjusts scroll position before paint. Historical
pages omit the former system prompt, so former current-segment message indexes
shift down by one; older historical indexes keep their coordinates. Newly keyed
rows above and inside the viewport are materialized before measuring, avoiding
`content-visibility` height estimates that would move the reading position. The anchor
uses the reader's latest position if they scroll during paging; sticky-bottom
readers continue following the tail. If a finite page budget excludes the
anchor, the nearest surviving row is used. The ordinary prepend anchor remains
responsible for non-compact scroll-up pages.

## Cross-compact paging

The first short viewport fills at most two older pages, stopping once content exceeds its height. After that, only an actual upward scroll movement classified by the sticky controller can load a page at the top. Layout changes, pin echoes, and following the bottom cannot page; a sustained upward gesture has a three-page budget, refilled by downward motion or a 750 ms arrival pause — the landing's own compensation echo is not downward motion. The spinner remains visible while a page is in flight. Deliberate scroll-up can continue through backend `has_more` and the configured `AVA_TIMELINE_COMPACT_HISTORY` depth.

While following the bottom, `display.timeline_retained_items_max` (250 by default) evicts the oldest selected-thread rows and keeps older paging available. A reader parked in history keeps the loaded and visible window until returning to the bottom. The temporary compact transition buffer is kept intact through its anchor handoff, then the canonical list is trimmed.

## Bounded mounted window

Long histories mount only the viewport plus one viewport of buffer on each side: [[ui/web/src/docs/frontend-components/timeline-view/bounded-window.ava.okf.md|Timeline bounded window]].

After a compact, `display.compact_history_sessions` controls automatic history pages above the new summary: 0 skips them, positive values fetch that many pages, and -1 walks all available pages serially. The store edge and retention hook live in [[ui/web/src/docs/frontend-state/timeline-cache.ava.okf.md|Selected Timeline State]].

## Deep collapse

`runs.classifyItem` classifies items as primary (agent replies + human inbound, always visible) / secondary (thinking / code / output, inter-agent messages, system inbound, compact, system_prompt, note marker) / bare (ephemeral marker). Adjacent secondary items form a `TurnBlock`; Details All/Last/None controls its default expansion, with Last opening the active final turn. In None mode an opened turn keeps its nested blocks collapsed (each opens on its own click); All/Last cascade the expansion into them (task #4780).

## Turn timer

The turn header's timer reads ONE basis in both states — `summarizeTurn.workedMs`, the sum of the turn's block durations — with the live "Working for" adding only the in-flight block's elapsed on top, so it does not drop when the turn ends. Wall-clock across a turn is never displayed: a turn is a maximal run of secondary items and can span an idle gap (a restart, a wake-up the agent had not picked up yet).

## Relationship to Other Nodes

- [[ui/web/src/docs/frontend-components/frontend-components.ava.okf.md|Frontend Components]] — the catalog this node was split out of; the sidebar, composer, and inspector live there.
- [[ui/web/src/docs/frontend-state/timeline-cache.ava.okf.md|Per-Thread Timeline Cache]] — the store that feeds this view, including the compact-replace edge and the retention re-attach.
