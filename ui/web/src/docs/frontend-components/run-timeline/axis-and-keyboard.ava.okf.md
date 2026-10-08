---
type: doc
title: Run timeline axis and keyboard
description: The run timeline's x map (time or hybrid token axis) and its arrow-key navigation.
tags:
- frontend
---

# Run timeline axis and keyboard

**Axis.** The x map (`buildAxisMap` in `timeline-model.ts`, a pure `AxisMap` every row, the context-size row, zoom, pan and ticks go through) has two modes, a toggle above the rows, plain time by default. Hybrid lays the layer-0 blocks end to end in read-time order: a block's width is its `context_tokens` (at least 0.3% of the total block weight, which is also what a block not yet in any request gets); the space between two blocks, and before the first and after the last up to the loaded extent, is `k * ln(1 + idle seconds)` with `k` set so all gaps together are 25% of the block weight; time is linear inside a block and inside a gap. A node spans the blocks its message range covers (its own times when none are loaded); events, requests and pending stretches go through the time map. Zoom and pan run in axis coordinates and convert back, so crumbs and drills stay time windows. Hybrid ticks are the start times of blocks, thinned to leave room for each label; time mode keeps round-number ticks.

**Keyboard.** Arrow keys (not in inputs or the resize handle) move the selection (`navigate`): left / right within a row; up to the parent (a block's level-1 node, a node's parent), down to the first child; with no parent or child, the item of the target row covering or nearest to the time (a request's bar goes up to its own block, between the two context rows to the same request); with no selection the leftmost item in view. The view pans to an item outside it, keeping its zoom (`revealView`).
