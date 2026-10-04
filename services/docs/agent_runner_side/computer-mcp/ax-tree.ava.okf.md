---
type: doc
title: "Computer-mcp — accessibility tools (ax_tree, ax_act)"
description: "The element-level path of computer-use: a bounded accessibility walk in the permissions helper, formatted and judged (quality verdict) by services/desktop/computer/ax_tools.py, stable element ids across walks, and ax_act element actions; the screenshot tools remain the explicit fallback."
tags:
- services
- computer-use
---

# Computer-mcp — accessibility tools (`ax_tree`, `ax_act`)

The element-level alternative to screenshot + OCR (`services/desktop/computer/ax_tools.py`).
The helper walks the target app's focused window through the macOS accessibility
API (`ax_tree`, gated on the Accessibility grant like click/type) — breadth-first,
bounded by a node cap, a depth cap, a time budget and a per-element messaging
timeout — and returns raw nodes in logical points. The daemon side is pure
formatting: `mode` filters (`interactive` controls plus their labels, `text`, or
`full`), unlabeled wrappers collapse, offscreen / zero-size / label-only-repeats
nodes drop, `max_nodes` caps the lines with a `... +N more under eK` marker that
`scope=eK` expands, and geometry becomes the element center in physical pixels
(the `click` space). `quality` is the verdict: `ok=false` (`no_window`,
`sparse`, `canvas`, `electron_not_exposed`) tells the caller to use the
screenshot tools instead — nothing falls back automatically. Secure text fields
are never echoed. A helper that predates the method (no `ax_tree_v1` in `ping`)
fails the tool with a rebuild instruction.

## Stable element ids
The helper numbers every walk's nodes afresh (raw ids) and stamps each with a
path fingerprint: parent fingerprint + role + identifier/title/description +
the ordinal among same-keyed siblings (values are excluded; a window keeps its
title). `services/desktop/computer/ax_ids.py` gives the same fingerprint the same
agent-visible id (`eN`) across walks, so a UI that shifted a little keeps its
ids; an element whose title changes is a new element. One table is live at a
time, for one app process: a different app or a restarted process starts a
fresh one, and an unscoped walk makes the elements it did not see unactionable
(their ids stay reserved so a scrolled-away row regains its id).

## Element actions (`ax_act`)
`ax_act(id, action, value?)` with `action` = `press` | `set_value` | `focus` |
`show_menu` acts through the accessibility API: no pointer movement, the target
app need not be frontmost (it does still take the screen lease and the action
lock). The helper acts by the raw id of its latest walk and answers `stale`
when the element is gone or its role / identifier / title / description changed
since it was read; the daemon then re-walks the app, re-finds the element by
fingerprint and acts exactly once more, else fails with "call ax_tree again" —
a wrong element is never acted on. An app that does not answer within the
timeout yields `completed=false` plus a note (the action may still have run).
`set_value` writes text into the field and is never echoed in the result, an
error or the `computer_action` audit row; the row carries the element center
and the action (`x,y,action`).

## Chromium-based apps and the visual gap
Electron, CEF and the Chrome family build their accessibility tree only when an
assistive tool is attached. For a bundle that ships one of those frameworks (or any framework with Chromium's
`Helpers/... Helper (Renderer).app` layout, which catches renamed forks such as
Lark) the
helper's first unscoped walk per process sets `AXManualAccessibility` on the app
(never `AXEnhancedUserInterface`, which VoiceOver owns and which changes native
window behavior), then waits, bounded, for the window to list children. The
switch is sticky in the target app until it quits and costs it some CPU;
`enable_ax=false` leaves the app untouched. `ax_enable` in the walk says what
happened (`n/a`, `off`, `set`, `already`, `failed`) and sharpens the quality
reason for a thin tree: `electron_ax_disabled`, `electron_enable_failed`,
`electron_not_exposed` (asked, still little), alongside `no_window`,
`unresponsive` (nothing readable in time), `canvas` and `sparse`.

`include_ocr_gap=true` (whole-window reads only) also OCRs the screen
(`services/desktop/computer/ax_gap.py`) and appends the text whose center falls in no
control or text element of the tree as `[px:N]` lines, the same fusion as UFO2's
UIA + vision merge. `px:` ids belong to that one call; the only action is
`ax_act(id="px:N", action="press")`, a click at the text center. An OCR failure
is reported beside the tree (`ocr_gap_error`), never failing the read.
