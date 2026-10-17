---
type: doc
title: Memory-Indexer — interval, backoff and pruning rationale
description: Why the patrol runs every 900 s with a 60 s doubling backoff capped at the interval and three retries, why rows outside the watched root are pruned, and why generated navigation files never enter the index
tags:
- services
- memory
---

# Memory-Indexer — interval, backoff and pruning rationale

## Interval and backoff

The 900 s interval, the 60 s doubling backoff (capped at the interval) and
`retry_limit=3` come from the shared pool's 90-day write history (≈150 file
changes/day; same-file edits within five minutes only 1.5%; batch consolidations
41.7%): a full content-hash sweep costs ≈0.27 s for 3013 files / 4.7 MB
(measured), so a minute-scale interval bounds staleness without measurable cost. The limit stops a
quota outage from spinning; the paths stay pending and visible in health.

These are code-level defaults in `MemoryIndexerConfig`, deliberately not an
environment knob: what counts as index content is a policy shipped with the
code, not an operational dial.

## Pruning rows outside the watched root

Rows whose path lies outside the watched root are pruned on the next pass: the
index is keyed by absolute path, and rows from an older authoring checkout would
otherwise still exist on disk and show every note twice in search.

## Generated navigation files

`MEMORY.md` and each directory's `index.md` are excluded by exact basename
(`excluded_basenames`, another code-level default in `MemoryIndexerConfig`):
consolidation regenerates them (16% of the pool's file changes over 90 days) and
they only list notes the index already holds. Rows they already have are pruned
by the next patrol.

## A vanished root fails the pass

A scan that finds no file while the index still holds paths fails the pass
instead of treating every row as deleted: the pass prunes nothing, the paths stay
pending and `/healthz` shows the failure. A root that disappears at runtime (for
example a home-directory migration moving the checkout) must never empty the
index; a genuinely empty pool has an empty index too, so it never trips this
guard.
