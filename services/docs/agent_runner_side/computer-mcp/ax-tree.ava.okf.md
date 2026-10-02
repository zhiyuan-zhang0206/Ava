---
type: doc
title: "Computer-mcp — accessibility tree (ax_tree)"
description: "The element-level read path of computer-use: a bounded accessibility walk in the permissions helper, formatted and judged (quality verdict) by services/computer/ax_tools.py; the screenshot tools remain the explicit fallback."
tags:
- services
- computer-use
---

# Computer-mcp — accessibility tree (`ax_tree`)

The element-level alternative to screenshot + OCR (`services/computer/ax_tools.py`).
The helper walks the target app's focused window through the macOS accessibility
API (`ax_tree`, gated on the Accessibility grant like click/type) — breadth-first,
bounded by a node cap, a depth cap, a time budget and a per-element messaging
timeout — and returns raw nodes in logical points. The daemon side is pure
formatting: `mode` filters (`interactive` controls plus their labels, `text`, or
`full`), unlabeled wrappers collapse, offscreen / zero-size / label-only-repeats
nodes drop, `max_nodes` caps the lines with a `... +N more under eK` marker that
`scope=eK` expands, and geometry becomes the element center in physical pixels
(the `click` space). Element ids (`eN`) index live references in the helper until
the next unscoped walk. `quality` is the verdict: `ok=false` (`no_window`,
`sparse`, `canvas`, `electron_not_exposed`) tells the caller to use the
screenshot tools instead — nothing falls back automatically. Secure text fields
are never echoed. A helper that predates the method (no `ax_tree_v1` in `ping`)
fails the tool with a rebuild instruction. Read-only: no element actions yet.
