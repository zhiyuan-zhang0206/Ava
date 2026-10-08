---
type: doc
title: Agent view axis and keyboard
description: The agent view's shared x map (time or hybrid token axis) and its arrow-key navigation across agents.
tags:
- frontend
---

# Agent view axis and keyboard

**Axis.** The x map (`buildSharedAxisMap` in `timeline-model.ts`, a pure `AxisMap` every row of every agent, zoom, pan and ticks go through; `buildAxisMap` is the one-agent case) has two modes, a toggle above the rows (Time | Token), plain time by default. Hybrid lays the layer-0 blocks of all agents end to end in read-time order: a block's width is its `context_tokens` (at least 0.3% of the total block weight, which is also what a block not yet in any request gets); the space between two blocks, and before the first and after the last up to the loaded extent, is `k * ln(1 + idle seconds)` with `k` set so all gaps together are 25% of the block weight; time is linear inside a block and inside a gap. Blocks are keyed by owner (agents share indices; `placerFor` hands each agent its view of the axis). A node spans the blocks of its own agent its message range covers (its own times when none are loaded); events, requests and pending stretches go through the time map. Zoom and pan run in axis coordinates and convert back. Hybrid ticks are the start times of blocks, thinned to leave room for each label; time mode keeps round-number ticks.

**Keyboard.** Arrow keys (not in inputs or the resize handle) move the selection (`navigate` in `timeline-nav.ts`): every row (Level N..1, Messages, Context size, Added context) is a list of items with an x extent on the axis, and one rule moves over all of them. Left / right: the adjacent item of the row. Up / down: the adjacent row's related item (a node's parent or first child, a block's level-1 node, the request that read a block and the blocks a request read, the same request in the other context row); with no relation, the item overlapping the current one most on the x axis, else the nearest. Up from an agent's first row (down from its last) continues in the previous (next) agent's nearest row, at the item overlapping the current one most on the axis (`navigateAcross` in `agent-view-nav.ts`; no kinship across agents, ids repeat). With no selection the leftmost item in view of the first agent that has one. Rows the settings hide are skipped. An arrow key also blurs the clicked block so its focus ring and hover echo do not linger. The view pans to an item outside it, keeping its zoom (`revealView`).

**Tokens.** Every node and block shows its context tokens right-aligned when it is wide enough (`tokenFits`; `~` marks an estimate); a node's `context_tokens` / `estimated` come from the backend (the sum over the messages of its span), never summed in the browser. The hover readout and the side panel show them too.
