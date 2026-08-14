---
type: doc
title: "Markdown Note Model"
description: '`shared/notes.py` — one parser for the whole repo: reads a `---`-frontmatter markdown note off disk, decides it is a note, and lifts the fields both graph consumers need. Note/walk_notes/extract_md_links/normalize_tags, plus the lenient type-preserving `parse_frontmatter_typed` in `shared/frontmatter.py`.'
tags:
- shared
- library
- markdown
---

# Markdown Note Model

## What it is

`shared/notes.py` is the single place that turns a `---`-frontmatter markdown
file into a typed `Note` — the model behind the memory pool concept graph
(`gateway/routers/memory.py`) and the OKF bundle graph
(`shared/okf_graph.py`). Before it existed, frontmatter parsing was inlined
three times (memory graph, memory search metadata, okf bundle) with two
different tags-normalization rules; now the delimiter scan lives once in
`shared/frontmatter.py:_split_frontmatter`, shared by the strict parser
(`parse_frontmatter`, skills/commands) and the lenient typed variant
(`parse_frontmatter_typed`, note pools).

## Contract

- `parse_note(text, rel) -> Note | None` — `None` when the file is not a note
  (no frontmatter, unterminated block, bad YAML, non-dict block). **One bad
  note is skipped, never fatal**: a note pool's contract is that a single
  malformed file must not take down the whole graph.
- `normalize_tags(raw)` — tags are a string list and nothing else: a bare
  string is wrapped, a list keeps only its string elements, anything else is
  `()` (`tags: 5` used to TypeError the graph endpoint).
- `walk_notes(root, skip_names=..., warnings=...)` — yields `(path, note)` in
  sorted order; unreadable files surface as warnings, reserved names and
  non-notes are skipped silently.
- `extract_md_links(body, source_dir, root, source_id)` — markdown links →
  `(source_id, target_id)` edges, resolved relative to the source file's
  directory; `http(s)`/`#`/empty targets skipped, and a link that resolves
  outside `root` is dropped instead of raising (an out-of-root link used to
  500 the whole `/api/memory/graph` endpoint).

## Notes

- `Note.rel` is the posix path relative to the pool root, sans extension — the
  stable node id both graph consumers key on. `Note.fm` keeps the raw
  frontmatter dict for callers that read fields the dataclass does not lift.
- Graph-specific semantics (primary_tag policy, edge filtering to known nodes)
  deliberately live in the callers, not here — this module stays schema-free.
