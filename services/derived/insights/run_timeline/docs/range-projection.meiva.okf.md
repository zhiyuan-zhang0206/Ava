---
type: doc
title: Run Timeline range projection
description: Run Timeline range projection.
tags: [contracts]
---

# Run Timeline range projection

`TimelineReader` (`projection/reader.py`, on `app.state.run_timeline_reader`) keeps
only up to 16 live segments, each pinned to an exact checkpoint and a complete
sealed prefix. A single SQL statement captures the head and its boundary count;
a missing indexed boundary returns a preparation response instead of combining
an older prefix with a newer tail. A head that is already a boundary is not
appended again. A snapshot fixes message history, while understanding summaries
may continue to grow within its message-count bound.

Sealed segments are projected once into `run_timeline_segments`,
`run_timeline_message_facts` and `run_timeline_blocks`. Facts retain corrected
opening/closing times and cumulative usage, context tokens, known counts and
estimated counts. Node queries locate candidate message endpoints using time
indexes, restrict spans in SQL, join endpoint facts (including a bounded live-tail
CTE), and aggregate generation costs only for returned job/check identities.
Blocks use SQL interval predicates; dense windows count without fetching payloads.
The block payload is UTF-8 JSON in BYTEA so legitimate NULs in historical previews
survive PostgreSQL storage. Full message bodies remain in the checkpoint store.

A segment covers the half-open global message range `message_start` to
`message_end` (exclusive). `local_start` skips only the repeated prompt in later
segments; the first segment retains its prompt. `offset = message_start -
local_start` maps every local message index to the stable global index used by
nodes, blocks and detail routes.

`IndexJobs` rotates pending agents one segment per turn, with one worker and at
most 16 admitted agents. Each segment and its facts/blocks publish atomically
under a transaction advisory lock. Queued/indexing/busy responses are 503 with a
retry hint. The transaction-scoped lock supports PgBouncer transaction pooling
and releases on failure without a separate unlock. Indexing failures are 500
and may be retried. HTTP requests never
perform whole-history reconstruction. The operator can prepare legacy agents
with `python -m services.derived.insights.run_timeline.projection.backfill 2697 6110`
after applying the migration through the authorized lifecycle. The executable
migration is `migrations/2026/10/16/15/20261016T153550_run-timeline-range-projection.sql`;
the earlier SQL under `db/migrations/` is retained historical source and is not
discovered by the migration loader. Use the installed
target generation and its explicit `MEIVA_HOME`; this writes derived tables only.
No request attempts DDL. A changed retained prefix fails explicitly and requires
an operator to rebuild the affected projection.

A snapshot is reused for five seconds, then its source head is checked; its
absolute lifetime is ten minutes. Unpinned snapshot capture uses fixed lock
stripes to serialize same-agent capture without an unbounded per-agent lock
dictionary. Window and detail reads carrying an opaque snapshot identity never
switch histories. Expired, replaced, evicted or wrong-agent IDs
return 409; the page reloads its owner and clears selections from the old cut.
Windows read no historical checkpoints. Raw message pages read only intersecting
segments; context reads the indexed next request's segment. Both reuse the cached
live tail. Each exact segment includes its own prompt and closing token anchor.

The projection reuses pure display/token owners. Each AI turn's context and
generation splits are shared within a derivation. An output block excludes its
opening AIMessage's tokens, which belong to the thinking/text/call blocks.
Compact replacement starts with a summary/request note that closes prior work.
OpenAPI descriptions are runtime schema metadata, preserving published docs
when internal source prose is migrated.


```meiva-contract
id = "indexed-history-windows"
kind = "invariant"
statement = "Prepared recent and panned windows query indexed rows without decoding historical checkpoints; details read only their own segment with stable indices, prompts and usage. Dense windows report an exact omitted count."
code = ["services/derived/insights/run_timeline/projection/derive.py:<module>", "services/derived/insights/run_timeline/projection/model.py:<module>", "services/derived/insights/run_timeline/projection/nodes.py:<module>", "services/derived/insights/run_timeline/projection/reader.py:<module>", "services/derived/insights/run_timeline/projection/source.py:<module>", "services/derived/insights/run_timeline/projection/store.py:<module>", "services/derived/insights/run_timeline/history.py:<module>", "services/derived/insights/run_timeline/router.py:<module>", "services/derived/insights/run_timeline/context.py:<module>", "services/derived/insights/run_timeline/messages.py:<module>", "services/derived/insights/run_timeline/tokens.py:<module>", "services/derived/insights/run_timeline/schemas.py:<module>"]
tests = ["services/derived/insights/run_timeline/tests/test_projection.py::test_cold_recent_range_and_pan_never_read_historical_bodies", "services/derived/insights/run_timeline/tests/test_projection.py::test_next_request_across_a_segment_boundary_fetches_only_its_own_segment", "services/derived/insights/run_timeline/tests/test_projection.py::test_dense_window_omits_all_detail_but_preserves_summaries", "services/derived/insights/run_timeline/tests/test_projection.py::test_compact_replacement_closes_work_before_the_next_segment", "services/derived/insights/run_timeline/tests/test_history_cache.py::test_recent_head_check_cannot_revive_an_absolutely_expired_snapshot"]
```

```meiva-contract
id = "bounded-projection-preparation"
kind = "invariant"
statement = "The request worker and operator backfill share atomic segment publication. Requested agents rotate after each segment; failed receipts cannot permanently fill admission. Shutdown stops new segments and reports unfinished work."
code = ["services/derived/insights/run_timeline/projection/jobs.py:<module>", "services/derived/insights/run_timeline/projection/backfill.py:<module>"]
tests = ["services/derived/insights/run_timeline/tests/test_projection_jobs.py::test_pending_agents_rotate_after_each_segment_and_admission_is_bounded", "services/derived/insights/run_timeline/tests/test_projection_jobs.py::test_unread_failures_do_not_consume_admission_and_explicit_retry_runs", "services/derived/insights/run_timeline/tests/test_projection_jobs.py::test_close_reports_an_active_segment_and_never_starts_queued_work"]
```
