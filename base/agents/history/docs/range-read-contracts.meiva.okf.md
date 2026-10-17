---
type: doc
title: History range read contracts
description: History range read contracts.
tags: [contracts]
---

# History range read contracts

The checkpoint store remains authoritative for bodies. Retained compaction boundaries
are append-only: their stitched offsets do not shift. An exact segment reader
preserves its own system prompt and closing token anchor, including delta
reconstruction; an unavailable checkpoint raises instead of becoming empty history.
An existing empty checkpoint is legitimate.

Understanding nodes without timestamps remain stored but are not served.
`hierarchy.store.SCHEMA_VERSION` must increase with the corresponding migration
when the stored hierarchy columns change.
Generation cost is recorded per chunk job or grouping check and is shared by all
nodes that call produced, rather than divided among them. Node-span usage comes
from AIMessage provider usage and recorded dollar cost only. Unknown costs are
not estimates. `input` includes cache tokens. Persisted prefix usage stays
unrounded so segment boundaries cannot change accumulation; span subtraction
rounds dollars at the existing nine-decimal precision.

Exact segment reads preserve heads and anchors; immutable segment offsets and unrounded usage prefixes keep window and full-history costs identical.
