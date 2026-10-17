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

```meiva-contract
id = "history-range-inputs"
kind = "invariant"
statement = "Exact segment reads preserve heads and anchors; immutable segment offsets and unrounded usage prefixes keep window and full-history costs identical."
code = ["base/agents/history/checkpoint_segment.py:<module>", "base/agents/history/hierarchy/usage.py:<module>", "base/agents/history/hierarchy/store.py:<module>"]
tests = ["services/derived/insights/run_timeline/tests/test_projection.py::test_cross_segment_nodes_equal_full_history_including_cost_and_zero_tokens", "base/agents/history/hierarchy/tests/test_hierarchy_usage.py::test_cost_sums_only_recorded_calls_and_counts_them", "base/agents/history/hierarchy/tests/test_hierarchy_store.py::test_generation_costs_are_summed_per_chunk_job_and_per_grouping_check"]
```
