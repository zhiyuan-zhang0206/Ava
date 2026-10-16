# Run Timeline reads indexed time ranges

## Scope

Reading every serialized checkpoint before filtering a recent-day response
took 46.8 seconds for agent 2697. The frozen verification fixture has 296
compaction segments, 98,130 messages and 147,752 display blocks (captured
slightly earlier than that timing). The operator needs the latest 24 hours of
activity on entry, older windows on pan, and exact message/context drill-down.
The cold read must remain fast after restarting Insights, not just after a
whole-history cache has warmed. Stored messages and their indices must not change.

## Prior art

PostgreSQL indexed read projections separate an immutable event store from its
query shape (CQRS). B-tree time indexes select display blocks; prefix sums give
constant-size endpoint reads for span costs. The existing checkpoint store and
its retained compaction boundaries remain the authority for message bodies.
No new database, cache service, serializer or queue service is necessary.

## Simplest sufficient design

Persist a versioned projection of each sealed segment: its global offset and
time extent, compact per-message facts (times and cumulative usage/token sums),
and display blocks. Derive each segment once, in order, and commit its rows in
one transaction. A process-local single worker catches up requested agents;
an explicit backfill entry point prepares legacy histories before rollout.
Requests never synchronously reconstruct an unindexed whole history: they
return a retryable preparation response until the projection is ready.

The latest unsealed segment is read at an exact checkpoint and derived once per
short-lived snapshot. Its size is bounded by the existing compaction policy.
Time-window reads select persisted blocks with SQL time predicates and merge
only that tail. Nodes join their endpoint facts and apply their corrected time
predicates in SQL; their bodies and usage records outside the window do not
cross the database boundary. Message expansion reads only the selected retained
segment(s); context expansion reads the segment of the indexed next request.

This does not solve a single unbounded uncompacted segment. It also cannot make
an unprepared legacy history instantly available: the operator must finish its
one-time backfill, or the UI explicitly shows preparation while it runs.

## Context and constraints

- **True:** messages are serialized checkpoint blobs; SQL cannot select a
  message timestamp from their contents. Boundaries are retained and stitched
  indices are append-only. Missing delta ancestry is an error, not empty history.
- **True:** node times are corrected by message read order and display extents;
  filtering the old raw node timestamps or checkpoint commit times is incorrect.
- **True:** clean compaction replacement starts with its own prompt and compact
  summary/request note, closing any earlier work unit. A tool result cannot
  continue a discarded work unit across that boundary; regression tests cover
  both ordinary and guarded replacement.
- **True:** compaction anchors and each segment's own system prompt determine
  token allocation. Splitting at arbitrary message pages loses that context.
- **Deliberate:** reuse the existing pure display/token derivation. A fixed
  production snapshot produced identical 147,752 blocks when derived segment
  by segment with the preceding segment's read-time floor.
- **Deliberate:** one indexing worker, one segment transaction at a time, bounded
  pending agents, no body uploads and no writes to source checkpoints.
- **Imagined, rejected:** range loading must retain the whole-history cache, or
  a longer HTTP timeout is a performance repair.

## Decision

Add three derived tables for segments, message facts and blocks. Include a
projection version in keys so a future representation can be prepared without
mutating another running generation's data. Index `(agent, version, time)` and
global message indices. Store no full message body in the projection. Block payloads use UTF-8 JSON in
BYTEA because historical previews can contain NUL, which PostgreSQL JSONB rejects.
Facts retain known/estimated counts as well as token sums and unrounded usage
prefixes. SQL merges only bounded live-tail endpoint facts; generation costs
are restricted to the returned nodes. Message history is pinned, not the
concurrently growing understanding tree.

Use the existing Insights process as owner. Its bounded catch-up worker and
operator backfill use the same idempotent segment writer. Per-agent transaction
advisory locks serialize competing writers. Head H, the boundary count through H
and its last published segment are captured by one SQL statement. The counts
must match before a snapshot is served; H itself is not appended if sealed.
Workers rotate after each segment so a legacy backfill does not starve a recent
compaction. Queued/indexing/busy are retryable; failures are explicit. A failed segment transaction leaves
the previous complete prefix readable and can be retried; no partial segment
is advertised. A snapshot fixes the sealed prefix and exact live checkpoint.
Expired snapshots return 409, so stale selections cannot silently point at a
different message. An absent snapshot requests a new one; malformed IDs fail.

Target a prepared, cold-process recent-day response under 3 seconds on Ubuntu,
including SQL and the live segment; verify independently, not as a guarantee
from warm-cache timing. Subsequent pan requests should read only indexed rows
in the selected interval. Keep the existing 35-second transport budget, at most
16 cached tails and a 10-minute snapshot lifetime. Dense windows omit layer-0
detail above the explicit block limit and retain summaries; zoom loads detail.

## Consequences

Historical reads no longer deserialize history or scale with serialized body
size. The projection consumes storage proportional to messages and blocks, but
its contents are reproducible. Backfill is an explicit deployment preparation
cost, distinct from HTTP latency. A first visit to an unprepared agent can need
preparation; errors are surfaced rather than rendered as an empty timeline.
Schema application and production backfill require an authorized rollout.

## Alternatives considered

1. **Full-history cache plus response windows:** fewer response bytes but the
   measured 46.8-second cold read remains. Rejected.
2. **Segment-time index, reconstruct all overlapping bodies on each request:**
   correct and smaller storage, but repeated pans still deserialize bodies and
   split large AI content; dense zoom-outs reconstruct all history. Persisting
   compact blocks prevents that already observed CPU failure.
3. **Normalize the authoritative message store:** permits direct body paging,
   but changes checkpoint writes and recovery semantics. A reproducible read
   projection fixes this consumer without changing the execution store.

## Validation before rollout

An isolated native PostgreSQL 17 database on Ubuntu held that frozen history,
with no production DDL or source-message writes. From a fresh application process,
the prepared recent-day handler took 0.230 seconds (0.259 including JSON), the
previous day 0.073 seconds, and a historical message expansion 0.078 seconds.
The recent call read one live checkpoint; the pan read none; expansion read one
selected historical checkpoint. This is a cold application process, not an OS
cache flush or production browser latency. Indexed SQL took 2.7 ms for recent
nodes and 3.2 ms for recent blocks in EXPLAIN ANALYZE. The one-time fixture import
and projection took 60.2 seconds. Independent whole-history derivation matched
all fields of 4,106 nodes and 147,752 units exactly.

Applying the migration to the previous schema produced the same projection and
node table definitions as a fresh schema. Native tests exercise competing
writers, failed-segment retry, dense windows, cross-segment token anchors and
snapshot eviction during a head check. The browser verified recent-day entry,
panning and historical message/context expansion against the isolated backend;
its shell, authentication and roster endpoints were fixtures. This does not
establish production rollout or production browser latency.
