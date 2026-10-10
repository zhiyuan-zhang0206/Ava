---
type: doc
title: Agent view arrows
description: The arrows between agents on the agent view, the Other agents group, and how an event lands on a row.
tags:
- frontend
---

# Agent view arrows

The page reads `GET /api/insights/run-timeline/links` for the agents in view over the extent they span together ([[services/derived/insights/run_timeline/docs/run_timeline_links.ava.okf.md]]). `model/timeline-links.ts` (pure) resolves each event into two ends; `canvas/run-timeline-links-canvas.tsx` draws them on one canvas over the whole chart (`pointer-events: none`, redrawn per frame; row positions are read from the DOM); the existing row canvases are untouched.

- **One look.** An arrow is a C-shaped cubic Bezier: both control points sit level with their end and are pushed to the right (later in time) by 45% of the vertical distance (28-140 px, so a straight-down link still bends), whether the link goes up or down, so all arrows lean the same way. A per-link factor of 0.76-1.24 (a hash of its key) changes only the size of the bend, so simultaneous links do not coincide. The arrowhead follows the curve's end tangent; hit testing measures the distance to the sampled curve. A kind is told apart by color only (message blue, spawn green, fork teal, terminate red, restart amber, resurrect purple). The legend under the chart lists the kinds with their count and switches each on or off.
- **Interactions switch.** A toolbar checkbox next to Context size, on by default. Off: no arrows, no link legend, no Other agents group (so the arrow keys never reach it) and no links read; on again restores each kind's own switch.
- **Ends.** Both ends of an in-view agent are in its Messages row; there is no lifecycle row. The sender stands at the event's time; the block it was working on is not guessed. The receiver is the block of the inbound row the event was delivered as (a chat message, and terminate, restart, resurrect or fork when their row is stamped on a block), matched by `inbound_id`; with no such block it is the event's time. When the event names an inbound row but no block carries it (a history from before the checkpoint stamped `ava_inbound_id`, or a note block), the details say the arrow is not matched to a block. An end outside the visible range is not drawn. Events of the user or the system are not drawn.
- **Other agents.** The last group is always there, with one row of events and no tree, Messages or Context rows. An event with one end in the view and the other not lands on this row at its time, as a colored tick; its arrow joins the in-view end. Adding the peer (a button in the event's details) moves the arrow into that agent's own group. The group cannot be removed.
- **Hover and select.** The nearest arrow within 4 px of the pointer is hovered, unless the pointer is over a block or node, which wins; the readout shows `kind · #A → #B · time`. A click selects the arrow and the details panel shows the event (a selected arrow and a selected block exclude each other). Down from the last agent's last row enters the Other agents row at the event nearest in time; left / right walk its events, up returns to the last agent's Messages row. Arrows between agents in view are reached by pointer only.
