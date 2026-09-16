# Timeline rendering and the history-window decision

## Context

The single-page frontend must remain responsive over HTTP/1.1 without adding a
certificate-management requirement. Only the selected agent needs detailed
live events. Durable history remains available on demand; history retention
and the amount of history mounted in a browser are separate decisions.

Timeline updates repeated historical grouping when only view state changed,
and sticky-header detection measured children of unrelated historical turns.
Neither operation required changing the document users could read or select.

## Decision

Reuse document grouping while its items are unchanged, and measure nested
sticky candidates only inside the active top-level turn. Preserve current
history paging, natural document order, native selection, and per-message
copy. Introduce no history cap, virtualizer, dependency, or alternate API.

This choice removes unnecessary work, not all work proportional to history.
Active loaded data and mounted DOM still grow with deliberately opened
history. Streaming updates still regroup changed items; the top-level sticky
candidate scan still covers the loaded document. These costs must remain
visible in performance claims.

## Alternatives considered

- **Retained document with incremental rendering:** preserves browser behavior
  for loaded, expanded text. Completed history can eventually retain stable
  render groups independently of the live tail. It does not guarantee bounded
  memory while a user keeps loading history.
- **Virtualized document with an evicting data window:** bounds mounted rows
  and, separately, resident history pages. Unmounted text is unavailable to
  native browser find; evicting DOM can interrupt native selection. A complete
  design needs durable-history search, an explicit copy/export scope, stable
  scroll anchors, and navigation to evicted ranges. Rendering virtualization
  alone does not bound loaded data.

An arbitrary item cap was rejected: silently discarding loaded rows would
confuse browser retention with durable-history availability and make the
existing older-history continuation misleading.

## Owner decision still required

Choose the history interaction contract before introducing virtualization:

1. Keep native browser find and cross-message selection over the entire loaded,
   expanded document, accepting memory proportional to deliberately loaded
   history; or
2. Adopt a bounded viewport/data window, with application-level durable-history
   search and explicit copy/export for ranges outside that window. Define
   whether active native selections pin their rows until selection ends.

For the second choice, define return-to-live and navigation to an evicted
newer range as well as older pagination. The server's older-history flag must
continue to describe durable history, independently of which rows happen to
be mounted or cached.
