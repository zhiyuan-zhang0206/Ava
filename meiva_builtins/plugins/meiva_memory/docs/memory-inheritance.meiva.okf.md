---
type: doc
title: Memory inheritance — the inheritable blocks read off the birth chain
description: meiva_memory's inherited-memory note — the meiva:inheritable fences an ancestor declares inside its personal entries, the birth-chain read behind them, the same-machine rule, the size guardrails, and the note assembly.
tags:
- plugin
- memory
- extensions
---

# Memory inheritance

An ancestor shares standing content with its descendants by fencing it inside a
personal memory entry (`<workspace>/memory/<slug>.md`) with an
`<!-- meiva:inheritable -->` … `<!-- /meiva:inheritable -->` pair. Every descendant
within `memory_inherit_depth` hops up the immutable birth chain
(`agents_meta.born_spawner`, nearest first) receives the blocks as a context note
when its window is established — cold start, after a compaction, or on a fork's
head rebuild (the fork strips the source's copy and grafts its own chain's).
The chain read is one light gateway round-trip (no tie graph), cached per
process: `born_spawner` is append-only, so a row's chain never changes. Entry
files are read off THIS machine's workspaces — an ancestor on another machine
is skipped and named in the footer (same-machine read, v1). Ranks, switches and
the window-side wiring are
[[agent/graph/docs/context-notes/memory-injection.meiva.okf.md|Memory Injection]]'s
to state.

## Block fences

Only the body of an entry file counts: a leading frontmatter block is skipped
via the pool validator's own split, so a marker inside frontmatter never opens a
block; CRLF files split like LF ones.

The inheritable blocks of one entry file: only the body counts — a leading frontmatter block is skipped via the pool validator's split — CRLF is normalized, and fences match line-anchored after strip; malformed fencing fails soft with warnings — an unclosed open fence drops its region without sweeping to EOF, a close without an open or a nested open is ignored — and an empty block declares nothing.

## The chain read

Read once per establishment; a failed read (or a host without a machine name)
degrades to no note with a warning and is not cached. Only ancestors whose
`agents_meta.machine` is this host contribute; an ancestor with no local store
is skipped quietly.

The note reads the first memory_inherit_depth ancestors off the immutable birth chain in one light gateway call, re-read at each establishment; a failed read degrades to no note with a warning and is not cached; only ancestors whose agents_meta.machine is this host contribute — a remote ancestor is skipped and named in the footer (same-machine read, v1) — and one with no local store is skipped quietly.

## Size guardrails

One oversized block must not balloon every descendant's context: the per-block
cap applies first, then the total budget clips the crossing section to its
remainder and omits the rest behind one marker; `0` disables either.

Size guardrails truncate oversized declarations with visible markers plus warnings: the per-block cap (memory_inherit_max_block_chars) first, then the total budget (memory_inherit_max_total_chars) clips the crossing section to its remainder and omits the rest behind one marker; 0 disables either; the lengths are the shared character measure.

## The note

One section per ancestor entry — nearest ancestor first, file names sorted, the
fences of one file joined into one section — behind the framing, with no
timestamps, so unchanged state renders byte-identical (the property the fork's
prefix-cache stability relies on). The assembled text is scanned like the shared
index and armed when flagged.

The note renders one section per ancestor entry — nearest ancestor first, file names sorted, the fences of one file joined into one section — behind the framing; the content carries no timestamps, so unchanged state renders byte-identical; it is None when the layer is off, no agent id is established, the chain read failed, or nothing was declared.

## Key Dependencies

- [[meiva_builtins/plugins/meiva_memory/docs/meiva_memory.meiva.okf.md]] — the owning plugin
- [[agent/graph/docs/context-notes/memory-injection.meiva.okf.md]] — the notes in the window: ranks, switches, fork behavior
- [[base/packages/docs/text-budget.meiva.okf.md]] — the shared metric and budget component
