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

During streaming each line is memoized `TimelineRow` + `React.memo` PythonCode/ChatMarkdown to suppress unchanged-row renders; `TimelineView` itself is memoized against unrelated page renders, and `TurnBlock` compares its rebuilt member ids and summary by value so collapsed historical turns skip each regroup. Every live clock (block chips, the turn header, the compacting block) shares one 100ms ticker in `reasoning-clock`; the interval exists only while a clock is live, and block clocks tick only while the agent is busy (`LiveClockGate`). The load-older circle animates while the reader pulls or a page is loading, and is absent while idle. Grouping is reused while the items reference is unchanged. Rows render with `aria-live="off"` (rows and history dividers opt out individually, so the direct-child structure used by scroll anchoring and dividers is unchanged); the view announces only turn-boundary changes, so streamed chunks generate no screen-reader noise. A live stamp that outlived a missed turn-end freezes on an idle page instead of re-rendering its row ten times a second. Sticky bottom auto-scroll + last-item fork.

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
Finishing removes only the ring, without moving messages. The position is saved before the pull hook's cleanup removes its native reserve. Older paging retains its separate top pull circle. Cold views
without retained rows keep the existing initial-load UI.

## Scroll ownership and pinned headers

While following the tail, content growth pins the viewport down in the same pre-paint tick, and the follow flag has a single owner (`lib/layout/sticky.ts:createStickyController`) fed by DOM events, with every performed scroll reported back so a pin's echo is a no-op. Expanded work and detail headers pin below HeaderBar and locally suppress under fast reader movement, with separate entry and exit speeds so it cannot flicker at the threshold. The pull reserve, the wheel and touch zones, and the scroll-anchoring opt-out live in [[ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.ava.okf.md|Timeline sticky scroll]].

## Continuous segments

Compact boundaries add no separator, label, gap, or virtual height to the message stream, and original messages remain readable in order across each boundary; during a transition the view renders buffered old groups above the new rank-0 window under provisional ranks until a single store update re-keys them. Prepend materialization, document-space anchor compensation and landing detection — including the no-double-count rule (#1272) and QA #3031 — are in [[ui/web/src/docs/frontend-components/timeline-view/continuous-segments.ava.okf.md|Timeline continuous segments]].

## Cross-compact paging

Position changes alone never request history: inertia, scrollbar moves, layout
clamps and prepend echoes all stop at the same boundary.


Earlier pages use a released native pull with a circular progress indicator and
stable prepend compensation. Gesture ownership, the prepared reserve and selected
row retention are described in [[ui/web/src/docs/frontend-components/timeline-view/history-paging.ava.okf.md|Timeline history paging]].

## Bounded mounted window

Long histories mount only the viewport plus one viewport of buffer on each side: [[ui/web/src/docs/frontend-components/timeline-view/bounded-window.ava.okf.md|Timeline bounded window]].

After a compact, `display.compact_history_sessions` controls automatic history pages above the new summary: 0 skips them, positive values fetch that many pages, and -1 walks all available pages serially. The store edge and retention hook live in [[ui/web/src/docs/frontend-state/timeline-cache.ava.okf.md|Selected Timeline State]].

## Deep collapse

Secondary items (thinking, code, output and the other detail kinds) group into TurnBlocks; Details All/Last/None drives their default expansion, and a turn's pinned expansion map plus opposite-of-default per-item overrides survive default flips and auto-collapse (#510). The classifier, the in-progress peel and the fork boundary are in [[ui/web/src/docs/frontend-components/timeline-view/deep-collapse.ava.okf.md|Timeline deep collapse]].

## Turn timer

The turn header's timer reads ONE basis in both states — `summarizeTurn.workedMs`, the sum of the turn's block durations — with the live "Working for" adding only the in-flight block's elapsed on top, so it does not drop when the turn ends. Wall-clock across a turn is never displayed: a turn is a maximal run of secondary items and can span an idle gap (a restart, a wake-up the agent had not picked up yet).

## Column geometry

The scroll surface is full-bleed — the scrollbar sits at the pane edge and the
gutters scroll — while the item column stays capped (`display.timeline_width_ratio`)
and centered inside it. The column clears the floating HeaderBar with 52px
(44px bar + 8px clearance, derived from `BAR_HEIGHT_PX`; user ruling 2026-08-06)
so a bar-height change cannot slide the first row underneath it. The composer
stack lives in normal flow below the surface (user ruling 2026-08-06), leaving
only a small bottom padding.

## Relationship to Other Nodes

- [[ui/web/src/docs/frontend-components/frontend-components.ava.okf.md|Frontend Components]] — the catalog this node was split out of; the sidebar, composer, and inspector live there.
- [[ui/web/src/docs/frontend-state/timeline-cache.ava.okf.md|Per-Thread Timeline Cache]] — the store that feeds this view, including the compact-replace edge and the retention re-attach.
- [[ui/web/src/docs/frontend-components/timeline-view/markers.ava.okf.md|System markers]] — system-marker dispatch and the fail-loud unrecognized alarm.
