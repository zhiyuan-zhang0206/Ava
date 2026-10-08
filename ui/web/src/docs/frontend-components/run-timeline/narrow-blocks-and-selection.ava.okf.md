---
type: doc
title: Run timeline canvas rows
description: How the timeline rows are painted on canvases, how crowded items collapse per pixel column, and how the selection stays visible at any zoom.
tags:
- frontend
---

# Run timeline canvas rows

**Canvas rows.** Every track (Level N..1, Messages, Context size, Added context) is one canvas (`run-timeline-canvas.tsx`) sized to its track in device pixels and repainted once per animation frame after each render, so a burst of zoom or pan events costs one paint. Where an item sits is pure (`timeline-canvas-model.ts`: `rowLayout`, cached per view by `layoutsFor`); what is drawn is `run-timeline-paint.ts`. Every color is opaque (a paler look is a `color-mix` toward the track, never layered alpha) and every edge is snapped to the device pixel grid, so the look is the same at any zoom.

**Level of detail.** An item at least `NARROW_DRAW_PX` wide is a block (border, rounded corners, summary text and token count when they fit). Narrower items collapse to one painted item per pixel column (`aggregateColumns`): the heaviest of the items touching the column stands for it (a bar row weighs by height, the others by how much of the column they cover), neighbouring columns of one item merge into a run, and the hairlines are drawn over the blocks.

**Pointer and keys.** A pointer position becomes the item under it by binary search (`hitTest`, hairlines answer first, within a pixel); hover, click, selection and the arrow keys reuse the same model as before (`timeline-nav.ts`). The canvas is decorative: the selected item is read out through a live region.

**Selection layer.** The selection is also its own layer: an outlined box at least 6 px wide in each row that holds a selected item (a selected request outlines its bars and the blocks it read), plus a thin vertical line through all rows over the whole selected extent (`selectionSpans`, `overlayBox`), kept as a few DOM elements above the canvases.
