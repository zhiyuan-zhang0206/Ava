---
type: doc
title: Run timeline details panel
description: The run timeline's page header and the side panel that shows the selected node or message block.
tags:
- frontend
---

# Run timeline details panel

**Agent heading.** `agent-view/agent-view-group.tsx` names each agent above its rows with its number and label as the conversation header names it (and a remove button when several are in view). It shows no status or model: the page reviews what happened, and both are the agent's present state.

**Side panel.** `run-timeline-detail.tsx` builds it from the inspector's `Section` and `Metric`. One Details section holds the time, message span and context tokens and, for a node, the agent's usage and cost over the span (calls, input, cache read / write, output, cost). The Markdown summary follows (`ChatMarkdown` at the panel's type scale). The understanding cost of a node (`generation`) is served but not shown. The Messages list (`run-timeline-messages.tsx`) renders each part through the conversation's own `TimelineRow` (card, header, `ItemView`), so type, size and color match the main page, and shows the context tokens the message occupies (`context_tokens`, a leading "~" when a share). A thinking, text or call block shows its own share of its AIMessage, not the whole message's.

**LLM request.** A thinking, text or call block of an AIMessage that was an LLM request (selected in either row) shows an LLM request section in its details: session, input, cache read / write, output and recorded cost, from `request` of the unit (repeated on each turn block of the message). The timeline itself draws no request.

**Side panel links.** A node's detail receives loaded ancestors nearest-first and loaded children one level below, and lists them as chips. A block's detail receives its covering level-1 node when loaded; a chip selects that node. Missing coverage is explicit.

**Messages height and Context size.** The Messages row draws every block bottom-aligned, in the block's type color. Its height is a setting (`UnitHeights`): equal, or by the block's tokens (`context_tokens`: a 6 px floor plus the rest of the height by the square root of the tokens, scaled to the largest block, so 10 / 100 / 300 token blocks stay apart next to a 20k one; the default). A click takes the block's whole x range over the row's full height, so a small block is easy to hit; the selection frame hugs what is drawn. The Context size row (a setting, off by default) draws one bar per block a request has read, as tall as the context through it (`context_total`, linear, scaled to the largest), sessions alternating in color, so a compaction reads as a drop. It holds the same blocks as the Messages row at the same x and width (the same layout, restricted to blocks with a total), so the two line up block for block; there is no per-message or per-request unit and no Added context row. A click on a bar selects its block (`{kind: "unit"}`): the block and its bar are linked in the other row, with the ancestors above. The details pane shows the block with, for a turn block of an AIMessage, an LLM request section (session, input, cache, output, cost). Hovering a block lights its ancestors. Arrows: a bar up selects its block in the Messages row, a block down its bar.

**Context card follows the point.** The card is not the agent's current context here: `contextPoint` picks a message index — a selected block's `i0`, a selected node's `span_start`, with nothing selected the last request sent inside the viewport (else the last before it) — and the card reads `GET .../run-timeline/context?at=` for the request at or after it, titled with its request, session and time (`placeholderData` keeps the last numbers while panning). The composer's panel still reads the current context.

Node details show recorded agent usage, not understanding-generation cost; missing recorded cost stays unknown. A turn-part selection shows only its matching parts and its own context-token share, while node spans show full messages. Raw messages are paged in groups of 50 with the selected snapshot. Conversation TimelineRow owns the message rendering. An arrow landing on a block uses that same message path; an unmatched event fetches only its selected content and renders it through PreviewMessage.
