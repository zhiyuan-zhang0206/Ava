---
type: doc
title: Timeline bounded window
description: How the conversation timeline mounts only the viewport plus a buffer for long histories — activation thresholds, height measurement, and reading-position pins.
tags:
- frontend
---

# Timeline bounded window

When the expanded display exceeds `display.timeline_window_activation_rows` (100 by default), `use-timeline-window` mounts the viewport plus one viewport of buffer on each side. Remote groups become two estimated-height spacers; children of an expanded `TurnBlock` use their own bounded window above `display.timeline_window_turn_rows` (100 by default). One ResizeObserver per tracking session records actual heights for mounted groups and rows (each commit observes only newly mounted nodes; the scroll listener and viewport observer are likewise installed once and read the latest groups and heights through refs), and the scroll position follows the last observed visible row when those estimates change. Group and child wrappers stay stable across activation; measurement starts at `display.timeline_window_measure_rows` (75 by default), capped below the activation count so an operator's lower activation setting still has a premeasurement interval. A lower value arriving after mount first gets a measured render, then activates the window on the next commit. All three values come from `/api/config` with baked fallbacks. The compact transition pins a rank-qualified item through its rekey and scroll transfer; load-older pins the exact reading item through prepend. Back/forward memory also saves the visible item, rank and viewport offset, with a collapsed-turn fallback. A short timeline keeps its existing CSS containment path. The canonical item list remains in the store for parked readers; only the DOM window is released. Following readers still use the separate 250-item retention rule.

Reading-row compensation and its parked window pin yield to the sticky controller
while following. A bottom-button, send, or switch command clears the parked row
so subsequent commits cannot cancel smooth scrolling or restore an old position.

History restores wait past the mount pin for layout to hold the target; scroll/send supersedes them.

## Relationship to Other Nodes

- [[ui/web/src/docs/frontend-components/timeline-view/timeline-view.ava.okf.md|Timeline View]] — the renderer this window bounds.
