# Shared Markdown: the safe HTML subset, lazy math and heading anchors

## Context

The 2026-10-15 decision
([`2026-10-15-markdown-context-and-file-references.md`](2026-10-15-markdown-context-and-file-references.md))
rendered the shared document through the common Markdown component
"without raw HTML": tags were silently dropped, and the surface had no math
and no heading anchors. The user's 2026-10-16 review of the reworked To user
panel asked for raw HTML to render conditionally, plus footnotes, formulas
and document anchors (flowcharts explicitly deferred), and approved
implementing the renderer work directly (2026-10-16, three-batch direction).

Prior art: react-markdown already owns the conversation and document
surfaces with remark-gfm and the CJK companions; an unresolved-URL guard
rewrites non-HTTP(S)/mailto/page-anchor URLs to `about:blank`; the backend
parses document links and images into resource references. The constraints:
the XSS boundary must stay with the existing sanitizer family, new
dependencies must be established mainstream picks
([technology selection](../../../conventions/technology-selection.md)), and a
heavy diagram renderer is out of scope.

## Decision

Render a safe HTML subset and extend the shared pipeline:

1. Raw HTML parses through `rehype-raw` and then the GitHub-style sanitize
   allowlist (`rehype-sanitize`), extended with `figure`/`figcaption`/`mark`;
   `script`, `style`, `iframe` and `template` are stripped with their
   content, and `style` and event-handler attributes never survive.
2. URLs resolve to their final form *before* the sanitizer judges them: the
   renderer rewrites every `a[href]` / `img[src]` (including raw HTML
   elements) through the document resolver, so a local scheme reaches the
   protocol check already authenticated; anything unresolved becomes
   `about:blank`, which the sanitizer drops — an unsafe URL never renders as
   a live link.
3. The sanitizer's clobber prefix is disabled: GFM footnote ids and
   `rehype-slug` headings author their own ids, and a prefix would
   desynchronize every id/anchor pair. scripts and styles are stripped and
   nothing resolves nodes through named globals, so the prefix buys nothing.
4. Math renders through `remark-math` + `rehype-katex` behind a lazy chunk:
   content without `$` never imports the math module, and KaTeX runs after
   the sanitizer with its trust/strict defaults and `throwOnError: false`.
5. Headings gain stable, deduplicated slug ids (`rehype-slug`) and
   same-document `#` links keep targeting them; the To user panel keeps
   page-anchor links in the panel.
6. The backend registers images and links inside raw HTML as resources too
   (`html.parser` over `html_inline`/`html_block` tokens), so the reference
   bookkeeping matches what renders.

This replaces the "without raw HTML" clause of the 2026-10-15 decision; the
file-reference and bound clauses stand.

## Alternatives rejected

- Keep `skipHtml` (drop all tags): the user explicitly asked for conditional
  raw HTML; report documents use `details`, `figure` and inline images with
  no alternative.
- Hand-rolled sanitizer or tag mapping through components: own the XSS
  boundary and the tag whitelist forever. The established ecosystem path
  (raw → sanitize) keeps the boundary with the library family the project
  already trusts.
- Rewrite URLs after the sanitizer: an unsafe URL would cross the sanitizer
  boundary as a live link before the resolver sees it; the protocol judgment
  must apply to the final URL.
- Keep the default clobber prefix and double-prefix/re-write anchors: GFM
  footnotes already carry the `user-content-` prefix (micromark) and
  `rehype-slug` authors plain ids; a rewriting plugin collides with them
  (verified during implementation), and no node is resolved through named
  globals once scripts and styles are stripped.
- Load KaTeX eagerly: content that never writes a formula would pay ~270KB
  of css/js on first paint; the module-level lazy import behind a `$` check
  avoids it.
- Mermaid flowcharts: a heavy renderer with its own security surface; not in
  this batch (separate evaluation).

## Consequences

Raw HTML inside the subset now renders where it was silently dropped —
documents may show new elements, and visual baselines were re-captured. Six
dependencies enter the frontend (`rehype-raw`, `rehype-sanitize`,
`rehype-slug`, `remark-math`, `rehype-katex`, `katex`); the user approved
the batch on 2026-10-16. The sanitizer remains the single security boundary,
pinned by tests including XSS counter-examples. Math cost stays conditional
on content; the backend's reference counts include raw HTML resources under
the unchanged document bounds. Flowcharts remain unsupported until a
separate decision.
