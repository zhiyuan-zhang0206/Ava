---
type: doc
title: Timeline View
description: The conversation timeline renderer (`components/timeline/`) — item kinds, memoized streaming rows, continuous segments, cross-compact scroll-up paging, and deep collapse.
tags:
- frontend
---

# Timeline View

`components/timeline/` renders the BackendTimelineItem list (chat / code / output / reasoning / system marker) and is the only item-list surface of a thread. Directory: `index`, `segments` (compact grouping), `use-compact-transition-anchor` (reading position), `use-timeline-window` (bounded DOM), `card`, `item`, `buttons`, `markers`, `timestamp`, `reasoning-clock`, `runs` (turn grouping), `run-block`.

## Streaming rows

During streaming each line is memoized `TimelineRow` + `React.memo` PythonCode/ChatMarkdown to suppress unchanged-row renders; `TimelineView` itself is memoized against unrelated page renders, and `TurnBlock` compares its rebuilt member ids and summary by value so collapsed historical turns skip each regroup. Every live clock (block chips, the turn header, the compacting block) shares one 100ms ticker in `reasoning-clock`; the interval exists only while a clock is live, and block clocks tick only while the agent is busy (`LiveClockGate`). The load-older circle animates while the reader pulls or a page is loading, and is absent while idle. Grouping is reused while the items reference is unchanged. Sticky bottom auto-scroll + last-item fork.

## Returning to an agent

The page keys each mounted timeline by agent and scopes its in-memory bookmark
by page-entry plus agent. A bookmark includes the visible message coordinate
and viewport offset; work/detail expansion choices are retained under the same
identity and Details mode. Returning materializes the prefix before restoring
that anchor. A former bottom follower returns to its last read item, including
when new messages arrived while away. Resize, tail repair and trim cannot move
that bookmark until the reader scrolls or explicitly requests the bottom.
Sending still jumps to the latest message; a first visit starts at the bottom.

The existing bottom button keeps one fixed footprint and its downward arrow.
A thin rotating ring runs along its edge while the latest read is queued/running,
including the composed re-attach read. The button stays enabled: clicking during
refresh jumps to the current bottom and resumes following incoming messages.
Finishing removes only the ring, without moving messages. Older paging retains its separate top pull circle. Cold views
without retained rows keep the existing initial-load UI.

## Header scrolling scope

Human-message and agent-reply titles stay in normal flow, including historical,
streaming and interrupted replies. Expanded work blocks retain their sticky
header below HeaderBar. Their expanded detail cards retain a second sticky line
below the work header, using its measured `--turn-header-h`. Collapsed headers
stay in flow. The existing frosted pane, geometry tracking and fast-scroll
suppression apply only to those work and detail headers.

Event timestamps remain visible in message-card headers at narrow widths;
headers and timestamp text wrap when needed. Standing context notes continue
to omit timestamps according to their timeline item metadata.

## Continuous segments

Historical ranks group separately for turn grouping and scroll anchors. Compact
boundaries add no separator, label, gap, or virtual height to the message stream.
Original messages remain readable in order across each boundary. Older pages
load after a fresh pull at the top ends, as described below.

During a compact transition, the view renders buffered old groups above the
new rank-0 window with provisional historical ranks and no visible boundary.
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

Prepend materialization keeps successfully measured rows above the reading
anchor at `content-visibility: visible`. Restoring `auto` immediately can revert
off-screen rows to intrinsic estimates after compensation; the preview's real
checkpoint reproduced a 378px shift. The bounded DOM window releases measured
rows as a long conversation scrolls. Abandoned landings restore their original
visibility styles.

## Cross-compact paging

Earlier pages use a released native pull with a circular progress indicator and
stable prepend compensation. Gesture ownership, the prepared reserve and selected
row retention are described in [[ui/web/src/docs/frontend-components/timeline-view/history-paging.ava.okf.md|Timeline history paging]].

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
