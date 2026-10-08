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
horizontal scroll pans, the tick axis follows; no gesture refetches. A click selects a block at once and fills the side panel
(`run-timeline-detail.tsx`: Markdown summary, time and message span, the agent's cost over the
span, the cost of generating the node, raw messages); a double-click or "Drill in" zooms the viewport to the block's span, selects
it and pushes a breadcrumb (`run-timeline-crumbs.tsx`; the root is the whole lifetime,
a crumb restores its viewport). A selection lights itself and every ancestor above it (`chainIds`: a node follows `parent`; a message block starts at its `parent`, the level-1 node covering it) and dims the rest. A level's row hatches the stretches the level above has not summarized yet (`pendingSpans`: the nodes one level down without a parent), so that tail reads as "not yet summarized" rather than empty; the blank between nodes that remains is a compaction segment's head. A block's body never reaches its row-neighbour's start (`layoutRow`, in track pixels): the 3 px minimum width only fills the free space before the next block, and a block with no room (a zero-duration node or a call point touching the next block) is a thin marker line with a small hit strip in its own top lane (coincident points stack in lanes); a marker highlights like a block. A pan release does not click the block under the pointer.
**Axis.** The x map (`buildAxisMap` in `timeline-model.ts`, a pure `AxisMap` every row, the context-size row, zoom, pan and ticks go through) has two modes, a toggle above the rows, hybrid by default. Hybrid lays the layer-0 blocks end to end in read-time order: a block's width is its `context_tokens` (at least 0.3% of the total block weight, which is also what a block not yet in any request gets); the space between two blocks, and before the first and after the last up to the loaded extent, is `k * ln(1 + idle seconds)` with `k` set so all gaps together are 25% of the block weight; time is linear inside a block and inside a gap. A node spans the blocks its message range covers (its own times when none are loaded); events, requests and pending stretches go through the time map. Zoom and pan work in axis coordinates and convert back to the time viewport, so crumbs and drills stay time windows. Hybrid ticks are the start times of blocks, thinned to leave room for each label (round times placed through the map when no block edge is in view); time mode keeps round-number ticks.
`timeline-model.ts` is the pure axis/viewport/drill model. `RunTimelineWorkspace`
splits main and side with the shared resizable wrapper at >=1280px (side panel 300-760px,
keyboard-resizable separator, ratio in localStorage `ava.run-timeline.split` through the
guarded `panelLayoutStorage`); below that the panel stacks under the chart. The
`ContextBreakdownCard` follows the chart.

**Legend highlight.** Each legend entry is a toggle (`run-timeline-legend.tsx`): pressing it highlights every block of that class (`Highlight`: class plus optional source) and fades the rest — other blocks and all summary blocks drop to 0.12 opacity (a selected block keeps its ring). The state lives on the page, so zoom, pan and drill keep it. While an inbound class (human / agent) is highlighted and its blocks come from several senders, a select narrows it to one source (`agent:N` reads "Inbound from agent N"). The context breakdown card's category rows that stand for a block class (user input, agent messages, thinking, text output, tool calls, tool responses, system notes) are the same toggle (`classCategory` / `categoryClass`).

**Hover.** A one-line readout above the rows (`run-timeline-readout.ts`) shows what the pointer is over: for a block its kind, message span, read time, source and an 80-character preview; for a node its level, time and message span, summary first line and the agent's own usage (calls, input, output); for a context-size bar the request. Hovering a block softly lights its ancestor chain; hovering a node lights its chain and the blocks its message span covers (`hoverLit`). A selection's own lighting wins over hover.

**Context size row.** `run-timeline-context-row.tsx` draws one bar per LLM request (`requests` of the response), as tall as its input tokens relative to the largest loaded; sessions alternate in color, so a compaction reads as a drop. A second row, Added context, draws per request what newly entered the context (`added_tokens` / `added_estimated` of `requests[]`, computed by the backend in `gateway/run_timeline/context.py`: the token sum of the messages first read by that request, i.e. from the previous request's AIMessage up to the message before this one, from the session's first message for a session's first request, the system prompt not counted). The two rows scale their heights independently, share the x map, and the hover readout of a request shows both numbers (an estimated addition ends with "(estimated)"; its bar is lighter).

**Side panel links.** A node's detail lists its loaded ancestors and child nodes as chips, a block's detail the summary block that covers it; a chip selects that node.

**Context card follows the point.** The card is not the agent's current context here: `contextPoint` picks a message index — a selected block's `i0`, a selected node's `span_start`, with nothing selected the last request sent inside the viewport (else the last before it) — and the card reads `GET .../run-timeline/context?at=` for the request at or after it, titled with its request, session and time (`placeholderData` keeps the last numbers while panning). The composer's panel still reads the current context.

**Sessions panel.** `run-timeline-sessions.tsx` (a collapsed section under the chart, so it takes no room from the rows; the list is read only when it is opened, `GET /api/agents/{id}/sessions`) lists the agent's sessions — the stretches between two compactions — with number, time range, message count, peak context, understanding coverage (none / partial / full with the ratio) and the estimated cost of describing the rest. A session's number zooms the timeline to its extent (a crumb "Sessions N"). Sessions are chosen by checkbox or by a from / to range; Estimate posts `POST /api/agents/{id}/understanding/build` with `dry_run` (jobs, tokens, cost, the cost basis; nothing is written) and Build is enabled only for the selection that was estimated. Build submits for real, then polls `GET .../understanding/builds/{id}` every 3 s (phase, each job's status and spend, the upper-level rebuild, total) until `done` / `failed`, once refreshing the timeline's nodes. With the understanding switch off (`understanding_enabled=false`) the panel says the build is queued and will not run until the switch is turned on.

- [[ui/web/src/docs/frontend-components/frontend-components.ava.okf.md|Frontend Components]] — the catalog this node was split out of.
