---
type: doc
title: Markdown Surfaces
description: Which frontend text renders through the shared ChatMarkdown surface and which stays verbatim monospace.
tags:
- frontend
---

# Markdown Surfaces

Text written as Markdown by a model or an author renders through the one `ChatMarkdown` surface (`components/markdown.tsx`; raw HTML disabled, safe links):

- Timeline: agent replies, reasoning (when `display.render_reasoning_markdown` is on), compact summaries, inter-agent `inbound_chat` bodies, impersonation messages, and the memory marker family (`memory` / `agent_memory` / `inherited_memory`).
- Open-notice and fleet-inbox content, memory-note bodies, and task descriptions / results (task graph hover card, kanban row).

Verbatim monospace `pre` stays for content that is not Markdown prose: human messages, command output, attach captions, compact requests, the system prompt, guidance-note markers, logs, JSON and code.
