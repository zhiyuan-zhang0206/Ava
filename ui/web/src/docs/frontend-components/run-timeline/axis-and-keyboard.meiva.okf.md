---
type: doc
title: Agent view axis and keyboard
description: The agent view's shared time axis and its arrow-key navigation across agents.
tags:
- frontend
---

# Agent view axis and keyboard

**Axis.** One linear time axis (`timeAxis` in `model/timeline-model.ts`, a pure `AxisMap` of milliseconds since the loaded extent's start) that every row of every agent, the context bars, zoom, pan and the round-number ticks go through. There is no other axis mode. A node spans its own start and end; blocks, events and requests go through the same map. Zoom and pan run in axis coordinates and convert back.

**Keyboard.** Arrow keys (not in inputs or the resize handle) move the selection (`navigate` in `model/timeline-nav.ts`): every row (Level N..1, Messages, Context size) is a list of items with an x extent on the axis, and one rule moves over all of them. Left / right: the adjacent item of the row. Up / down: the adjacent row's related item (a node's parent or first child, a block's level-1 node, a block and its bar in the Context size row, both ways); with no relation, the item overlapping the current one most on the x axis, else the nearest. Up from an agent's first row (down from its last) continues in the previous (next) agent's nearest row, at the item overlapping the current one most on the axis (`navigateAcross` in `agent-view/agent-view-nav.ts`; no kinship across agents, ids repeat). With no selection the leftmost item in view of the first agent that has one. Rows the settings hide are skipped. An arrow key also blurs the clicked block so its focus ring and hover echo do not linger. The view pans to an item outside it, keeping its zoom (`revealView`).

**Tokens.** Every node and block shows its context tokens right-aligned when it is wide enough (`tokenFits`; `~` marks an estimate); a node's `context_tokens` / `estimated` come from the backend (the sum over the messages of its span), never summed in the browser. The hover readout and the side panel show them too.

**Gesture ownership.** A native non-passive wheel handler prevents the page from scrolling during zoom. Its live viewport advances before React renders, and an older render cannot overwrite an unrendered wheel result. A primary-button move becomes a pan after 4 px; pointer capture keeps the gesture on the chart. Releasing a pan suppresses the following click. Items win over crossing arrows for hover and click, and event-row markers own their own hover. Selection and hover repaint cached row layouts rather than changing item geometry.

The canvas tests provide measured track rectangles because the test DOM does not lay out the chart. The wheel helper sends a MouseEvent carrying the cursor position plus explicit wheel deltas; this preserves the coordinates needed to verify cursor-anchored zoom. The drag test supplies pointer capture on its chart element.

```meiva-contract
id = "row-gesture-and-navigation"
kind = "invariant"
statement = "Wheel events compose against the latest unrendered viewport, so bursts do not repeat a stale zoom. Panning suppresses the release click; movement below the drag threshold remains a click. Items under the pointer win over arrows crossing them. Keyboard navigation crosses visible agent rows and then event groups, while editable and resize controls keep their own keys. The primary selection is framed strongly; linked rows receive lighter frames and narrow selections receive a track hairline."
code = ["ui/web/src/components/run-timeline/run-timeline-rows.tsx:RunTimelineRows", "ui/web/src/app/insights/run/[agents]/page.test.tsx:wheel"]
tests = ["ui/web/src/app/insights/run/[agents]/page.test.tsx::zoom and pan::wheel events that arrive before React renders compose instead of repeating the old view", "ui/web/src/app/insights/run/[agents]/page.test.tsx::zoom and pan::a drag pans and its release does not select the block under the pointer", "ui/web/src/app/insights/run/[agents]/page.test.tsx::zoom and pan::a press that barely moves is still a click", "ui/web/src/components/run-timeline/agent-view/agent-view.test.tsx::arrows between agents::shows the arrow under the pointer in the readout, and a block over it wins", "ui/web/src/components/run-timeline/agent-view/agent-view.test.tsx::agent view::walks the arrow keys from one agent's last row into the next agent's first, and back", "ui/web/src/components/run-timeline/agent-view/agent-view.test.tsx::arrows between agents::walks the Other agents row with the arrow keys, and enters it down from the last agent", "ui/web/src/components/run-timeline/run-timeline-rows.test.tsx::RunTimelineRows keyboard and context bars::moves the selection with the arrow keys and ignores them in an input", "ui/web/src/components/run-timeline/run-timeline-rows.test.tsx::RunTimelineRows narrow items::frames an instant exactly as wide as it is drawn (3 px) and marks it with a faint line in the tracks only"]
```
