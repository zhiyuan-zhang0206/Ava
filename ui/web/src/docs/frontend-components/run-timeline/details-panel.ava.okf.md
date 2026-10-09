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

**Side panel links.** A node's detail lists its loaded ancestors and child nodes as chips, a block's detail the summary block that covers it; a chip selects that node.
