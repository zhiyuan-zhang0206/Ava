---
type: doc
title: Markdown Surfaces
description: Which frontend text renders through the shared ChatMarkdown surface and which stays verbatim monospace.
tags:
- frontend
---

# Markdown Surfaces

Text written as Markdown by a model or an author renders through the one `ChatMarkdown` surface (`components/markdown.tsx`; raw HTML disabled, safe links):

- To user: root and linked Markdown documents, with raw HTML skipped and local
  references resolved through the authenticated document resource API.
- Timeline: agent replies, reasoning (when `display.render_reasoning_markdown` is on), compact summaries, inter-agent `inbound_chat` bodies, impersonation messages, and the memory marker family (`memory` / `agent_memory` / `inherited_memory`).
- Open-notice and fleet-inbox content, memory-note bodies, and task descriptions / results (task graph hover card, kanban row).

Verbatim monospace `pre` stays for content that is not Markdown prose: human messages, command output, attach captions, compact requests, the system prompt, guidance-note markers, logs, JSON and code.

```ava-contract
id = "markdown-rendering"
kind = "invariant"
statement = "Raw HTML never executes. The default URL policy permits HTTP(S), mailto and page anchors; explicit document resolvers may replace registered local references with authenticated resource URLs. Both GFM tilde tokenizers disable single-tilde strike-through so home paths and approximations stay literal next to CJK text. Fenced code retains whitespace even without a language tag, has one pre wrapper, and exposes copy controls. Memoization avoids re-parsing unchanged sibling content during streaming."
code = ["ui/web/src/components/content/markdown.tsx:<module>"]
tests = ["ui/web/src/components/content/markdown.test.tsx::ChatMarkdown::no-language fence renders as a <pre> block, not inline (newlines kept)", "ui/web/src/components/content/markdown.test.tsx::ChatMarkdown::javascript: URL rewritten to about:blank by urlTransform (XSS guard)", "ui/web/src/components/content/markdown.test.tsx::single tilde stays literal (task #3653)::only the double form strikes in a mixed line"]
```
