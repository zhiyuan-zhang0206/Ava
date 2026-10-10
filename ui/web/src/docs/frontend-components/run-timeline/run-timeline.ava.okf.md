---
type: doc
title: Agent View
description: The agent view page — any number of agents on one shared zoomable axis, each with tree-level rows and a lane of typed message blocks, with the side panel for the selected node or block.
tags:
- frontend
---

# Agent View

`/insights/run/{ids}` (`405` or `405,6657`; the URL follows the agents added and removed, nothing else is kept) draws any number of agents on one timeline; one agent is the view of that agent alone, there is no second page. All agents are equal (no root or lineage), each is a group of rows under its `Agent #id · label` heading with a remove button (never on the last agent), and none is squeezed to one line. A toolbar adds an agent by id and sets two things for all groups: how many tree levels to draw (counted from each agent's topmost level; all by default) and the context bars (Context size row on or off, Messages height equal or by tokens; off and by tokens by default). Each agent is read by its own `GET /api/agents/{id}/run-timeline`; the loaded extent is the union of their windows, a failed agent shows a retry in its own group. Arrows between agents, the Other agents group and their legend are in [[ui/web/src/docs/frontend-components/run-timeline/agent-links.ava.okf.md]]. Per agent
(`components/run-timeline/`): one row per tree level, topmost first, layer 0 (the
message blocks) at the bottom on one lane. A block is one of inbound (human / agent), agent text, thinking, tool call, tool output or a framework note; a work unit is drawn as its thinking, call and output blocks (the backend splits it, `units.display_blocks`), colored with the context breakdown's palette (`lib/context-colors.ts`) and listed in a legend; clicking one shows only that part of the raw message. The page
reads the agent's whole lifetime once; all rows share one viewport over that loaded data
(`run-timeline-rows.tsx`): wheel / pinch zooms at the cursor (down to 100 ms), drag or
horizontal scroll pans, the tick axis follows; no gesture refetches. A click selects a block at once and fills the side panel ([[ui/web/src/docs/frontend-components/run-timeline/details-panel.ava.okf.md]]). There is no drill-in and no breadcrumb: zoom and pan are the only viewport moves. A selection lights itself and every ancestor above it (`chainIds`: a node follows `parent`; a message block starts at its `parent`, the level-1 node covering it; a request starts at the parent of every block it read) and dims the rest. What a level has not summarized yet is left blank, as is a compaction segment's head. A block's body never reaches its row-neighbour's start (`layoutRow`, in track pixels): the 3 px minimum width only fills the free space before the next block, and a block with no room (a zero-duration node or a call point touching the next block) is a thin marker line with a small hit strip in its own top lane (coincident points stack in lanes); a marker highlights like a block. A pan release does not click the block under the pointer.
**Axis and keyboard.** The shared time axis (`timeAxis`) and the arrow-key navigation across agents are in [[ui/web/src/docs/frontend-components/run-timeline/axis-and-keyboard.ava.okf.md]].
`model/timeline-model.ts`, `model/timeline-nav.ts` and `agent-view/agent-view-nav.ts` are the pure axis and navigation models. A selection is `{agent, selection}`: node ids and block indices are only unique within an agent. The details panel and the context card follow the selected agent (else the first). `RunTimelineWorkspace`
splits main and side with the shared resizable wrapper at >=1280px (side panel 300-760px,
keyboard-resizable separator, ratio in localStorage `ava.run-timeline.split` through the
guarded `panelLayoutStorage`); below that the panel stacks under the chart. The
`ContextBreakdownCard` follows the chart.

**Source layout.** `components/run-timeline/model/` owns the pure axis, selection, row layout and hover readout models with their tests. `canvas/` owns rendering, painting, the axis and the shared canvas test helper. `agent-view/` owns agent headings, shared controls and navigation across agents with their tests. Page composition, rows and the details panel stay at the component root; consumers import their owning modules directly.

**Lifetime reads.** The lifetime read uses the shared 35-second HTTP read budget, covering both
request headers and response-body consumption. Each query passes its cancellation
signal to the network request: removing the agent or leaving the page cancels
that read. A timeout follows the existing per-agent failure and Retry flow;
selection cancellation does not become a business failure. This client deadline
does not establish a backend history-reconstruction deadline.

**Legend highlight.** Each legend entry is a toggle (`run-timeline-legend.tsx`): pressing it highlights every block of that class (`Highlight`: class plus optional source) and fades the rest — other blocks and all summary blocks drop to 0.12 opacity (a selected block keeps its ring). The state lives on the page, so zoom and pan keep it. While an inbound class (human / agent) is highlighted and its blocks come from several senders, a select narrows it to one source (`agent:N` reads "Inbound from agent N"). The context breakdown card's category rows that stand for a block class (user input, agent messages, thinking, text output, tool calls, tool responses, system notes) are the same toggle (`classCategory` / `categoryClass`).

**Hover.** A one-line readout above the rows (`model/run-timeline-readout.ts`) shows what the pointer is over: for a block its kind, message span, read time, source and an 80-character preview; for a node its level, time and message span, summary first line and the agent's own usage (calls, input, output); for a block also the context through it. Hovering a block softly lights its ancestor chain; hovering a node lights its chain and the blocks its message span covers (`hoverLit`). A selection's own lighting wins over hover.

**Messages height and Context size.** The Messages row draws every block bottom-aligned, in the block's type color. Its height is a setting (`UnitHeights`): equal, or by the block's tokens (`context_tokens`: a 6 px floor plus the rest of the height by the square root of the tokens, scaled to the largest block, so 10 / 100 / 300 token blocks stay apart next to a 20k one; the default). A click takes the block's whole x range over the row's full height, so a small block is easy to hit; the selection frame hugs what is drawn. The Context size row (a setting, off by default) draws one bar per block a request has read, as tall as the context through it (`context_total`, linear, scaled to the largest), sessions alternating in color, so a compaction reads as a drop. It holds the same blocks as the Messages row at the same x and width (the same layout, restricted to blocks with a total), so the two line up block for block; there is no per-message or per-request unit and no Added context row. A click on a bar selects its block (`{kind: "unit"}`): the block and its bar are linked in the other row, with the ancestors above. The details pane shows the block with, for a turn block of an AIMessage, an LLM request section (session, input, cache, output, cost). Hovering a block lights its ancestors. Arrows: a bar up selects its block in the Messages row, a block down its bar.

**Context card follows the point.** The card is not the agent's current context here: `contextPoint` picks a message index — a selected block's `i0`, a selected node's `span_start`, with nothing selected the last request sent inside the viewport (else the last before it) — and the card reads `GET .../run-timeline/context?at=` for the request at or after it, titled with its request, session and time (`placeholderData` keeps the last numbers while panning). The composer's panel still reads the current context.

- [[ui/web/src/docs/frontend-components/frontend-components.ava.okf.md|Frontend Components]] — the catalog this node was split out of.
