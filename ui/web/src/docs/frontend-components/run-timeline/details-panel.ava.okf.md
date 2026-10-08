---
type: doc
title: Run timeline details panel
description: The run timeline's page header and the side panel that shows the selected node or message block.
tags:
- frontend
---

# Run timeline details panel

**Page header.** `run-timeline-header.tsx` fills the shared `PageHeader` (`components/shell/page-header.tsx`, Fleet's header geometry; the cluster view uses it too) with the agent's number and label as the conversation header names it. It shows no status or model: the page reviews what happened, and both are the agent's present state.

**Side panel.** `run-timeline-detail.tsx` builds it from the inspector's `Section` and `Metric`. One Details section holds the time, message span and context tokens and, for a node, the agent's usage and cost over the span (calls, input, cache read / write, output, cost). The Markdown summary follows (`ChatMarkdown` at the panel's type scale). The understanding cost of a node (`generation`) is served but not shown. The Messages list (`run-timeline-messages.tsx`) renders each part through the conversation's own `TimelineRow` (card, header, `ItemView`), so type, size and color match the main page, and shows the context tokens the message occupies (`context_tokens`, "(estimated)" when a share). A thinking, text or call block shows its own share of its AIMessage, not the whole message's.

**Side panel links.** A node's detail lists its loaded ancestors and child nodes as chips, a block's detail the summary block that covers it; a chip selects that node.
