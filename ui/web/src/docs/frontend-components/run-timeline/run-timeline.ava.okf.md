---
type: doc
title: Run Timeline
description: The agent run timeline page — tree-level rows and one lane of typed message blocks on a shared zoomable axis, with the side panel for the selected node or block.
tags:
- frontend
---

# Run Timeline

`/insights/run/{id}` draws the agent's understanding tree and message history
(`components/run-timeline/`): one row per tree level, topmost first, layer 0 (the
message blocks) at the bottom on one lane, lifecycle markers from the audit record on top. A block is one of inbound (human / agent), agent text, thinking, tool call, tool output or a framework note; a work unit is drawn as its thinking, call and output blocks (the backend splits it, `units.display_blocks`), colored with the context breakdown's palette (`lib/context-colors.ts`) and listed in a legend; clicking one shows only that part of the raw message. The page
reads the agent's whole lifetime once; all rows share one viewport over that loaded data
(`run-timeline-rows.tsx`): wheel / pinch zooms at the cursor (down to 100 ms), drag or
horizontal scroll pans, the tick axis follows; no gesture refetches. A click selects a block at once and fills the side panel ([[ui/web/src/docs/frontend-components/run-timeline/details-panel.ava.okf.md]]). There is no drill-in and no breadcrumb: zoom and pan are the only viewport moves. A selection lights itself and every ancestor above it (`chainIds`: a node follows `parent`; a message block starts at its `parent`, the level-1 node covering it; a request starts at the parent of every block it read) and dims the rest. What a level has not summarized yet is left blank, as is a compaction segment's head. A block's body never reaches its row-neighbour's start (`layoutRow`, in track pixels): the 3 px minimum width only fills the free space before the next block, and a block with no room (a zero-duration node or a call point touching the next block) is a thin marker line with a small hit strip in its own top lane (coincident points stack in lanes); a marker highlights like a block. A pan release does not click the block under the pointer.
**Axis and keyboard.** The x map (time or hybrid token axis, `buildAxisMap`) and the arrow-key navigation are in [[ui/web/src/docs/frontend-components/run-timeline/axis-and-keyboard.ava.okf.md]].
`timeline-model.ts` and `timeline-nav.ts` are the pure axis and navigation models. `RunTimelineWorkspace`
splits main and side with the shared resizable wrapper at >=1280px (side panel 300-760px,
keyboard-resizable separator, ratio in localStorage `ava.run-timeline.split` through the
guarded `panelLayoutStorage`); below that the panel stacks under the chart. The
`ContextBreakdownCard` follows the chart.

**Legend highlight.** Each legend entry is a toggle (`run-timeline-legend.tsx`): pressing it highlights every block of that class (`Highlight`: class plus optional source) and fades the rest — other blocks and all summary blocks drop to 0.12 opacity (a selected block keeps its ring). The state lives on the page, so zoom and pan keep it. While an inbound class (human / agent) is highlighted and its blocks come from several senders, a select narrows it to one source (`agent:N` reads "Inbound from agent N"). The context breakdown card's category rows that stand for a block class (user input, agent messages, thinking, text output, tool calls, tool responses, system notes) are the same toggle (`classCategory` / `categoryClass`).

**Hover.** A one-line readout above the rows (`run-timeline-readout.ts`) shows what the pointer is over: for a block its kind, message span, read time, source and an 80-character preview; for a node its level, time and message span, summary first line and the agent's own usage (calls, input, output); for a context-size bar the request. Hovering a block softly lights its ancestor chain; hovering a node lights its chain and the blocks its message span covers (`hoverLit`). A selection's own lighting wins over hover.

**Context size row.** The context rows (canvas, `run-timeline-paint.ts`) draw one bar per LLM request (`requests` of the response), as tall as its input tokens relative to the largest loaded; sessions alternate in color, so a compaction reads as a drop. A second row, Added context, draws per request what newly entered the context (`added_tokens` / `added_estimated` of `requests[]`, computed by the backend in `gateway/run_timeline/context.py`: the token sum of the messages first read by that request, i.e. from the previous request's AIMessage up to the message before this one, from the session's first message for a session's first request, the system prompt not counted). The two rows scale their heights independently (Added context by square root, Context size linearly), share the x map, and the hover readout of a request shows both numbers (an estimated addition ends with "(estimated)"; its bar is lighter). A request bar spans the blocks the request read for the first time: `added_from` / `added_to` of `requests[]` (a half-open message-index range from the backend) are mapped to the Messages row's units (`requestUnits`), and the bar runs from the first block's start to the last block's end on either axis (`requestSpan`, `barBox`: 1 px gap, min 2 px), so it sits under its messages. Request bars are buttons: a click selects the request (`{kind: "request"}`), which lights the bar and every block it read and points the Context Breakdown at it; the details pane shows the block of the AIMessage that made it (`requestSelection`). Hovering a request lights its blocks, and selecting or hovering a block lights the bar of the request that read it. Arrows: a bar up selects the last block it read, a block down the request that read it.\n\n

**Context card follows the point.** The card is not the agent's current context here: `contextPoint` picks a message index — a selected block's `i0`, a selected node's `span_start`, with nothing selected the last request sent inside the viewport (else the last before it) — and the card reads `GET .../run-timeline/context?at=` for the request at or after it, titled with its request, session and time (`placeholderData` keeps the last numbers while panning). The composer's panel still reads the current context.

- [[ui/web/src/docs/frontend-components/frontend-components.ava.okf.md|Frontend Components]] — the catalog this node was split out of.
