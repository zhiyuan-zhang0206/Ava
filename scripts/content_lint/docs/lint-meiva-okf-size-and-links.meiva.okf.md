---
type: doc
title: OKF node lint — size ceiling, wikilink edges and concatenation defects
description: The size limits, graph edges and glued-marker defects enforced by scripts/content_lint/lint_meiva_okf.py; the scan and schema/placement rules are in its sibling node.
tags: [scripts, lint, documentation]
---

# OKF node lint — size ceiling, wikilink edges and concatenation defects

The `.meiva.okf.md` checks in `scripts/content_lint/lint_meiva_okf.py` beyond
placement: how large a node may grow, what its `[[wikilinks]]` may point at and
which text shapes are blocked. The scan, schema and placement rules are
[[scripts/content_lint/docs/lint-meiva-okf.meiva.okf.md]].

## Size ceiling

Each node is measured in characters against `MAX_CHARS` with a non-blocking
headroom band (`WARN_MARGIN` below the ceiling), and in lines against
`MAX_LINES` — a backstop against a pathological many-short-lines document;
the character ceiling is the one that binds at this repo's prose density.
Past a ceiling the lint reports E007 and fails the run; inside the band it
prints W010 with the size and the room left, before the wall, while a split is
still cheap to plan. Every message names the remedy — split a section into its
own node, with the directory that child belongs in. The constants move only as
a deliberate change in the same PR, justified in the commit message; the raise to
8000 and the band are recorded in
[the ceiling decision](../../../docs/decisions/engineering/design/simplification/2026-07-29-okf-node-ceiling.md).

Each node is measured in characters against a repo-wide hard ceiling with a non-blocking headroom band that reports the size and the room left, and in lines against a line-ceiling backstop; a breach reports E007 and fails the run, the message names the split-out child's directory, and the constants move only as a deliberate change in the same PR, justified in the commit message.

## Wikilink edges

`[[wikilinks]]` are the node-graph edge syntax and their universe is the
`.meiva.okf.md` files — nothing else. Targets resolve through the graph
builder's resolver; a miss reports W008, and when the target names a real
non-node markdown file on one of the other three doc axes
(`docs/decisions/`, `future/`, `docs/conventions/`) the message says so,
because the remedy is a normal markdown link. That search looks at the target
as a repo-root path, then by basename across the repo, skipping hidden and
heavy directories rather than walking them. A target that carries a
directory component must denote the node it resolves to: the unique-basename
fallback is reported (W011) while the link still works, so the path is fixed
rather than discovered broken later. Code samples are not links — inline code
spans and fenced blocks are masked out of the scan.

Wikilinks resolve only inside the .meiva.okf.md node graph, through the graph builder's resolver; a miss is reported, a target that names a real non-node markdown file on the decision / plan / convention axes is reported with that remedy, and a directory-qualified target that resolves only by unique-basename fallback is reported while the link still works. Inline code spans and fenced blocks are not edges.

## Concatenation defects

A header or bullet marker glued directly onto the text before it — no blank
line, no newline between them — renders as part of the previous line instead
of its own block; this is the defect class the W010 okf-split campaign
produced, and the same header line twice in a row is its other shape. Both
are blocking defects (E012, E013). Detection runs on the raw text so line
numbers are exact, masks fenced blocks and inline code spans with a
line-preserving blank, and is anchored on punctuation immediately before the
marker — an inline mention like "C#" or a header's own repeated `#` does not
fire.

A header or bullet marker glued directly onto the preceding text with no blank line, and the same header line twice in a row, are blocking defects; detection runs on the raw text with line-preserving masking of fenced blocks and inline code, anchored on punctuation so an inline mention like C# and a header's own repeated hash run do not fire.

Parent: [[scripts/content_lint/docs/content_lint.meiva.okf.md]].
