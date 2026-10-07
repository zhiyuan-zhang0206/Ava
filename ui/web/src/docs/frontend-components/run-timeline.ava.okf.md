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
`timeline-model.ts` is the pure axis/viewport/drill model. `RunTimelineWorkspace`
splits main and side with the shared resizable wrapper at >=1280px (side panel 300-760px,
keyboard-resizable separator, ratio in localStorage `ava.run-timeline.split` through the
guarded `panelLayoutStorage`); below that the panel stacks under the chart. The
`ContextBreakdownCard` follows the chart unchanged.

- [[ui/web/src/docs/frontend-components/frontend-components.ava.okf.md|Frontend Components]] — the catalog this node was split out of.
