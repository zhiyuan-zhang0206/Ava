---
type: doc
title: Timeline continuous segments
description: How compact boundaries and prepends keep the reading position stable — buffered transition groups, content-visibility materialization, document-space anchor compensation, and landing detection.
tags:
- frontend
---

# Timeline continuous segments

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

Prepend materialization forces `content-visibility: auto` rows above the reading
anchor to render until the anchor stops moving: a single pass under-counts rows
whose estimate gap is smaller than the accumulated growth (#2623), and measured
rows stay `visible` because restoring `auto` can revert them to the 80px
estimate (the preview's real checkpoint reproduced a 378px shift after
compensation). The bounded DOM window releases measured rows as a long
conversation scrolls; abandoned landings restore their original visibility
styles. The anchor is the topmost visible real node (standing context is skipped
— older history inserts after it), and its document-space top
(`rect.top + scrollTop`) is invariant under user scrolls: the landing
compensates the anchor's actual displacement since the previous commit, so a
fetch landing after the reader scrolled, or windows landing back-to-back, cannot
double-count (a viewport-space scheme could drag the reading position by the
reader's own travel — #1272). Landing is signalled by the item id at the array
front (the first item that is not standing context): once a turn-collapse
boundary sits at the anchor, a prepend can extend that run's front while its
rendered node class stays the same, which DOM identity cannot separate. The
landed lookup tries the anchor's exact item row first and only falls back to its
enclosing collapsed turn — matching both in one selector would let the earlier
turn shadow the row. A zero or negative displacement means the content above was
replaced (compact reset, truncated snapshot), not a landing: the anchor is
abandoned instead of jumping the viewport. A load cycle that ends without a
landing releases the capture, or the pending anchor would block every later pull
(QA #3031). A landing's compensation write is marked and consumed by the next
scroll event, so it is never read as reader motion by paging or the header
release.

## Restore settling

A bookmark applied on re-entry lands with whatever heights the window has
measured so far; the rows around the anchor finish measuring afterwards, and
each correction moves everything below it. The view therefore keeps re-asserting
the anchor's saved viewport offset while the layout settles instead of trusting
the one-shot landing, and it records no bookmark until a layout change observes
the anchor at that offset -- a window that cannot show its anchor cannot save one
faithfully, so a switch away inside the settle keeps the previous bookmark.
Wheel, key, touch, scrollbar and send hand the position back to the reader and
end the guard; recording then resumes immediately.

```ava-contract
id = "restore-settles-its-anchor"
kind = "invariant"
statement = """An applied reading bookmark keeps re-asserting its saved anchor offset while the rows around it settle, and no position is recorded while a restore is pending or unsettled. The guard releases when a layout change observes the anchor at its saved offset, when the entry shows different content, or when the reader takes over (wheel, key, touch, scrollbar, send); a switch away inside the settle therefore keeps the saved bookmark."""
code = ["ui/web/src/components/timeline/model/restore-settle.ts:settleSavedTimelineScroll"]
tests = ["ui/web/src/components/timeline/history-scroll.test.tsx::restored position settles::keeps a restored reading item at its saved offset while the rows above settle", "ui/web/src/components/timeline/history-scroll.test.tsx::restored position settles::keeps the saved bookmark while the restored window is still settling", "ui/web/src/components/timeline/history-scroll.test.tsx::restored position settles::keeps the bookmark through a switch away inside the settle"]
```

## Relationship to Other Nodes

- [[ui/web/src/docs/frontend-components/timeline-view/timeline-view.ava.okf.md|Timeline View]] — the renderer whose segments and anchors this node owns.
