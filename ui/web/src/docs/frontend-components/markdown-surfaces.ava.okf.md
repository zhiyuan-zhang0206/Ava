---
type: doc
title: Markdown Surfaces
description: Which frontend text renders through the shared ChatMarkdown surface and which stays verbatim monospace.
tags:
- frontend
---

# Markdown Surfaces

Text written as Markdown by a model or an author renders through the one `ChatMarkdown` surface — a lazy boundary (`components/content/markdown.tsx`) over the renderer (`components/content/markdown-renderer.tsx`; raw HTML disabled, safe links):

- To user: root and linked Markdown documents, with raw HTML skipped and local
  references resolved through the authenticated document resource API.
- Timeline: agent replies, reasoning (when `display.render_reasoning_markdown` is on), compact summaries, inter-agent `inbound_chat` bodies, impersonation messages, and the memory marker family (`memory` / `agent_memory` / `inherited_memory`).
- Open-notice and fleet-inbox content, memory-note bodies, and task descriptions / results (task graph hover card, kanban row).

Verbatim monospace `pre` stays for content that is not Markdown prose: human messages, command output, attach captions, compact requests, the system prompt, guidance-note markers, logs, JSON and code.

```ava-contract
id = "markdown-rendering"
kind = "invariant"
statement = "Raw HTML never executes, and urlTransform rewrites every non-HTTP(S)/mailto/page-anchor URL to about:blank; external HTTP(S) links open in a new tab with rel=noopener noreferrer, and explicit document resolvers may replace registered local references with authenticated resource URLs. remark-cjk-friendly, plus the remark-cjk-friendly-gfm-strikethrough companion required while remark-gfm is in the pipeline, restores bold and strikethrough when delimiters sit against CJK letters or punctuation-led content (curly quotes, straight quotes, corner brackets — task #3314); plain CJK and ASCII emphasis stays unchanged. Both GFM tilde tokenizers disable single-tilde strike-through so home paths and approximations like the reported ~61G and ~/work stay literal, only the double form strikes, and tildes inside inline code are untouched (task #3653). Fenced code retains whitespace even without a language tag, renders as one pre block, and exposes copy controls; a fenced python block routes to the PythonCode surface while inline code never does. Memoization avoids re-parsing unchanged sibling content during streaming. The suite mocks PythonCode, so the tests never run Prism."
code = ["ui/web/src/components/content/markdown-renderer.tsx:<module>"]
tests = ["ui/web/src/components/content/markdown.test.tsx::ChatMarkdown::no-language fence renders as a <pre> block, not inline (newlines kept)", "ui/web/src/components/content/markdown.test.tsx::ChatMarkdown::javascript: URL rewritten to about:blank by urlTransform (XSS guard)", "ui/web/src/components/content/markdown.test.tsx::single tilde stays literal (task #3653)::only the double form strikes in a mixed line", "ui/web/src/components/content/markdown.test.tsx::CJK emphasis (remark-cjk-friendly, task #3314)::renders bold for the reported sentence (curly quotes)"]
```

```ava-contract
id = "markdown-lazy-loading"
kind = "invariant"
statement = "ChatMarkdown is a lazy boundary: the renderer (the react-markdown chain) is a deferred chunk, so route bundles carry only the boundary and a cold first paint shows the same content as plain text inside the shared chat-md wrapper. A resolved renderer renders Markdown on the first paint; a pending one upgrades the mounted tree when it arrives, and a superseded or unmounted attempt never writes back. Prefetch starts when the boundary module evaluates and is shared and deduplicated by one module-level promise; a rejected load clears the cache, keeps the fallback, and the next attempt retries."
code = ["ui/web/src/components/content/markdown.tsx:<module>"]
tests = ["ui/web/src/components/content/markdown-lazy.test.tsx::ChatMarkdown lazy boundary::shows the plain-text fallback while the chunk is in flight, then upgrades to markdown", "ui/web/src/components/content/markdown-lazy.test.tsx::ChatMarkdown lazy boundary::renders markdown on the first paint when the renderer was already prefetched", "ui/web/src/components/content/markdown-lazy.test.tsx::ChatMarkdown lazy boundary::keeps the fallback after a rejected chunk load and recovers on the next attempt"]
```
