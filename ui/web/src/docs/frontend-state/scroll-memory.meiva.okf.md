---
type: doc
title: History-Entry Scroll Memory
description: Per-container reading positions keyed by the history entry's bfcacheId — saved on every scrolling surface, restored before paint, dropped on a content-key mismatch.
tags:
- frontend
---

# History-Entry Scroll Memory

The document never scrolls (h-full chain; every page scrolls inside its own
element), so the browser's history scroll restoration has nothing to restore:
a back/forward remount would reset each container (timeline re-pinned to the
newest message, terminal to its tail). Every scrolling surface saves its
position under the history entry it belongs to — keyed by
`router.bfcacheId`, which the client router keeps across back/forward,
`router.refresh()`, and search-param-/hash-only navigations and hands out
fresh only when a push/replace lands on a new segment (vendored Next 16.3.4
use-router docs), independent of `cacheComponents` — so a restore happens
only on a return to a kept entry; a first visit keeps its own default, and a
search-param-only switch (e.g. `?agent_id=`) shares the previous home
visit's slot. The record carries a content key (agent / agent+session): a
mismatch drops the position instead of restoring it onto other content, and
the sticky flag rides along so a follower returns following. Restores land in
a layout effect on the first commit that can hold them (before paint), report
through
`notifyRestored` (following resumes exactly at an at-bottom restore), and the
mount force-pin defers to a pending restore.

Parent: [[ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md]].
