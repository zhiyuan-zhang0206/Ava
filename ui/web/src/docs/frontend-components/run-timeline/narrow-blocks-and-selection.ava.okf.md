---
type: doc
title: Run timeline canvas rows
description: How the timeline rows are painted on canvases, how crowded items collapse per pixel column, and how the selection stays visible at any zoom.
tags:
- frontend
---

# Run timeline canvas rows

**Canvas rows.** Every track (Level N..1, Messages, Context size, Added context) is one canvas (`canvas/run-timeline-canvas.tsx`) sized to its track in device pixels and repainted once per animation frame after each render, so a burst of zoom or pan events costs one paint. Where an item sits is pure (`model/timeline-canvas-model.ts`: `rowLayout`, cached per view by `layoutsFor`); what is drawn is `canvas/run-timeline-paint.ts`. Every color is opaque (a paler look is a `color-mix` toward the track, never layered alpha) and every edge is snapped to the device pixel grid, so the look is the same at any zoom.

**Level of detail.** An item at least `NARROW_DRAW_PX` wide is a block (border, rounded corners, summary text and token count when they fit). Narrower items collapse to one painted item per pixel column (`aggregateColumns`): the heaviest of the items touching the column stands for it (a bar row weighs by height, the others by how much of the column they cover), neighbouring columns of one item merge into a run, and the hairlines are drawn over the blocks.

**Pointer and keys.** A pointer position becomes the item under it by binary search (`hitTest`, hairlines answer first, within a pixel); hover, click, selection and the arrow keys reuse the same model as before (`model/timeline-nav.ts`). The canvas is decorative: the selected item is read out through a live region.

**Selection.** The model splits a selection into one primary item (the item under the cursor, in the row the cursor is in: the two context rows show the same message, so only one of them is primary) and the items linked to it (`selectionRoles`): a node links to its ancestors, a block to its ancestors and the messages it shows (both context rows), a message to the block(s) that show it, their ancestors and itself in the other context row. Links go one hop and never back down from an ancestor, so a message's ancestors do not light the other messages they cover. The arrow keys move only the primary. The primary gets a 2 px accent frame hugging its drawn box, a pixel off the item; the linked items of a row share one light dashed 1 px frame around the whole batch. Items keep their color; everything outside the selection loses some contrast; hover is lighter still. A frame is exactly as wide as what is drawn; a primary item narrower than 6 px also gets a faint hairline in the tracks. Blocks show no token count: the side panel does.

**Minimum width.** The axis stays linear: an item starts exactly where its time is, and only its drawn width is at least `MIN_ITEM_PX` (3 px), in every row alike (`layoutSpans`), so instants (inbound, heartbeat, a call point) are visible. Items closer than that overlap, and the column merge above paints each pixel column once, so colors never deepen. Two instants overlap when they are less than 3 px of the view apart, i.e. under `3 * viewSpan / trackPx` of time (about 70 s on a 6.5 h view over 1000 px; the gap shrinks as you zoom in).
