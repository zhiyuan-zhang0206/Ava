---
type: doc
title: Markdown safe content
description: The ChatMarkdown renderer's sanitized HTML subset, lazy KaTeX math and document anchors — allowlist, URL resolution order, id handling and the tests that pin them.
tags:
- frontend
---

# Markdown safe content

The renderer's content extensions, split out of
[[ui/web/src/docs/frontend-components/markdown-surfaces.meiva.okf.md]]: what raw
HTML may render, how math arrives, and how in-document anchors line up.

## The safe HTML subset

Allowed raw HTML renders; a tag outside the allowlist unwraps to its children,
and `script`, `style`, `iframe` and `template` are stripped with their content.
`style` and event-handler attributes never survive. URLs resolve before the
sanitizer judges them, so a scheme the allowlist does not name can still become
its final authenticated URL; anything unresolved falls back to `about:blank`,
which the sanitizer then drops — an unsafe URL never renders as a live link.
react-markdown repeats the resolver after the sanitizer (its own `post()` pass),
so the resolver must return an already-resolved absolute `http(s)` URL
unchanged — a relative result would be rewritten to `about:blank` by that
second pass.
Ids render as authored: GFM footnote ids and rehype-slug headings author their
own, and the sanitizer's clobber prefix would desynchronize every id/anchor
pair, so it is disabled (scripts and styles are stripped, and nothing resolves
nodes through named globals).

Raw HTML renders through the GitHub-style sanitize allowlist extended with figure/figcaption/mark; script, style, iframe and template are stripped with their content, style and event-handler attributes never survive, and other non-allowlisted tags unwrap to their children. The resolver rewrites every a[href] / img[src] to its final URL before sanitizing, so file: and other local schemes reach the protocol check resolved; an unresolved value becomes about:blank, which the sanitizer drops, so an unsafe URL never renders as a live link. Ids render as authored — GFM footnote ids and rehype-slug headings author their own and the clobber prefix is disabled — while scripts and styles are stripped and nothing resolves nodes through named globals.

## Math

Inline `$x$` and display `$$…$$` math render through KaTeX, loaded lazily:
content without a `$` never imports the math chunk, and KaTeX output renders
after the sanitizer — its inputs cannot produce injection and its trust and
strict defaults stand.

Math renders through KaTeX only after a lazily loaded chunk: content without $ never imports remark-math / rehype-katex (the loader is the unit under test), content with $ upgrades to inline and display math once the deduplicated module promise resolves, and KaTeX runs after the sanitizer with throwOnError false — its inputs cannot produce injection and its trust/strict defaults stand.

## Document anchors

Headings gain stable slug ids — deduplicated — and same-document links target
them; GFM footnotes keep every anchor pair matching after sanitizing; the To
user panel keeps page-anchor links in the panel.
