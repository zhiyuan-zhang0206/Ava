---
type: doc
title: Run timeline narrow blocks and selection overlay
description: How zoomed-out rows merge sub-pixel blocks into one fill and how the selection stays visible at any zoom.
tags:
- frontend
---

# Run timeline narrow blocks and selection overlay

**Narrow blocks and selection.** A block drawn under `NARROW_DRAW_PX` gets no border or rounding, and the narrow blocks of a row that touch the same pixel column are painted as one cell (`mergeNarrow`, drawing only: each block stays its own button for select, hover and navigation), so a zoomed-out row does not darken where blocks pile up. The selection is its own layer: an outlined box at least 6 px wide in each row that holds a selected item (a selected request outlines its bars and the blocks it read), plus a thin vertical line through all rows over the whole selected extent (`selectionSpans`, `overlayBox`).
