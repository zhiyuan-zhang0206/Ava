---
type: doc
title: Agent view axis and keyboard
description: The agent view's shared time axis and its arrow-key navigation across agents.
tags:
- frontend
---

# Agent view axis and keyboard

**Axis.** One linear time axis (`timeAxis` in `timeline-model.ts`, a pure `AxisMap` of milliseconds since the loaded extent's start) that every row of every agent, the context bars, zoom, pan and the round-number ticks go through. There is no other axis mode. A node spans its own start and end; blocks, events and requests go through the same map. Zoom and pan run in axis coordinates and convert back.

**Keyboard.** Arrow keys (not in inputs or the resize handle) move the selection (`navigate` in `timeline-nav.ts`): every row (Level N..1, Messages, Context size, Added context) is a list of items with an x extent on the axis, and one rule moves over all of them. Left / right: the adjacent item of the row. Up / down: the adjacent row's related item (a node's parent or first child, a block's level-1 node, the messages a block shows and the block(s) that show a message, the same message in the other context row); with no relation, the item overlapping the current one most on the x axis, else the nearest. Up from an agent's first row (down from its last) continues in the previous (next) agent's nearest row, at the item overlapping the current one most on the axis (`navigateAcross` in `agent-view-nav.ts`; no kinship across agents, ids repeat). With no selection the leftmost item in view of the first agent that has one. Rows the settings hide are skipped. An arrow key also blurs the clicked block so its focus ring and hover echo do not linger. The view pans to an item outside it, keeping its zoom (`revealView`).

**Tokens.** Every node and block shows its context tokens right-aligned when it is wide enough (`tokenFits`; `~` marks an estimate); a node's `context_tokens` / `estimated` come from the backend (the sum over the messages of its span), never summed in the browser. The hover readout and the side panel show them too.
