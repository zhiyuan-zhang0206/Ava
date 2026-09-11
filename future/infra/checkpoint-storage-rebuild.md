# Checkpoint storage rebuild — model design + schema migration plan

Date: 2026-09-11
Author: #6095 (checkpoint-storage-design, task #3097)
Requester: #405 (user ruling 2026-09-11 17:53)
Status: **design draft — no code**. Read against main@d41321bdb; production numbers are
read-only, as-of 2026-09-11 18:02-18:04 CST (live table — snapshots, not constants).

## 0. Summary

The 2026-09-11 incident: an agent attached seven inlined screenshots (1-2 MB each) to its
conversation; every retained checkpoint re-serialized the whole message list, so each write
grew to ~25 MB, cross-network writes hit the 1-minute `statement_timeout`, and the turn
stalled permanently. The mechanism is general: **every retained checkpoint rewrites every
state channel in full**, so cumulative bytes written over a thread's life grow quadratically
with its content, and the single-write size equals the full state size.

This document designs the storage-model change and its migration, staged:

- **T1 — large-payload externalization (recommended first):** payloads over a threshold
  (images, big tool outputs) leave the channel blobs and live once in a content-addressed
  store; blobs carry references. No change to the full-snapshot channel model, no new
  framework dependency. It removes the incident class (multi-MB rewrites, duplicated images),
  doubles as the repair path for existing over-limit threads, and keeps every current
  invariant (trim, fork, traces) intact with a GC pass added.
- **T2 — delta storage for `messages` (gated, evaluate after T1):** upstream langgraph 1.2.4
  ships `DeltaChannel` (beta) plus a delta-aware Postgres saver; measured here at ~14x less
  write volume when no snapshots are forced (4.8x with periodic snapshots). It needs
  retention snapshot-forcing, an n-step-wrapper rework, a fork fix, and a read-path audit
  before it is safe. Beta status + version pin make it a separate, explicitly approved step.
- **T3 — retention snapshot-forcing (required iff T2):** before the reaper trims ancestors,
  materialize a `_DeltaSnapshot` at the surviving checkpoint (langgraph `prune` option 2),
  so keep-latest-K retention stays correct. Without it, trimming a delta thread silently
  severs the ancestor chain and state reconstructs empty (demonstrated in Section 3.4).
- **Migration:** expand-contract with paired `.down.sql`, reader-first rollout ordering, a
  batched backfill, and the new writer taking over only after old-code writers are drained —
  following the existing two-phase rollout pattern (workspace-6087
  `bootstrap-manual-final.md` semantics).

The near-term write guard (task #3096 / #6094, PR #2226) landed via the merge queue on
2026-09-11T17:51:21Z — one second before the user's recall — and was fully reverted by user
ruling the same night (task #3139): no cap or downscale mitigation is in the codebase, and
Section 6's exit list reflects that. Over-limit threads (today: 6093, 6089) remain writable
as before — slowly.

## 1. Current state (measured)

### 1.1 Write path mechanics

- Saver: `PooledPostgresSaver` (`services/agent_host/pooled_checkpoint.py`) subclasses
  langgraph's `AsyncPostgresSaver`; one logical saver per host, per-cursor pool leases.
- Serde: `build_checkpoint_serde()` -> `JsonPlusSerializer` with the framework msgpack
  allowlist (`agent/state.py`). In production, every blob row is `type='msgpack'`.
- Durability: default `"async"` — one checkpoint row per super-step
  (node boundary); a turn spans several super-steps.
- N-step wrapper `_wrap_saver_writes_with_nstep_interval` (`agent/startup.py:171`,
  interval = `AVA_CHECKPOINT_INTERVAL`, default 4):
  - retained steps (`step % 4 == 0`) and non-`loop`/`update` sources (input, fork) write;
    skipped steps write nothing.
  - On retained/flush writes it merges the checkpoint's full `channel_versions` into
    `new_versions` (`_versions_with_current_blobs`, `agent/startup.py:153`). Because the
    saver writes a blob for every channel listed there (values are present in
    `channel_values`), **each retained checkpoint persists the current full value of every
    channel whose version changed since the last write** — this fixed a dangling-reference bug but is exactly the full-rewrite
    amplifier.
  - A turn ends with `_ava_nstep_flush(thread_id)` persisting the last skipped checkpoint.
    Per-thread locks serialize writes and flushes.
- Net: a checkpoint's write volume = the full serialized state at that step; `messages`
  (the whole conversation list) dominates.

### 1.2 Measured per-checkpoint volumes (scratch Postgres, real saver + wrapper code)

Harness: local scratch cluster, `AsyncPostgresSaver` + `build_checkpoint_serde()` +
`_wrap_saver_writes_with_nstep_interval` (imported from `agent/startup.py`), one message
pair of known size appended per super-step (image case: one incompressible 25 MB payload).
Byte counts are `octet_length` of inserted rows (raw; `pg_column_size` undercounts
compressible content because TOAST pzips it).

| Case | steps | interval | final content | total blob bytes written | max single blob write |
|---|---|---|---|---|---|
| A  long-thread shape | 120 | 4 | 758 KB | 11.75 MB (15.5x final) | 758 KB |
| A2 same, 2x steps | 240 | 4 | 1.52 MB | 46.24 MB (30.5x final) | 1.52 MB |
| C  no-throttle control | 120 | 1 | 758 KB | 45.86 MB (60.5x final) | 758 KB |
| B  one 25 MB image at step 20 | 40 | 4 | 25.09 MB | 150.5 MB (6x final) | 25.09 MB |

- Doubling steps multiplies cumulative bytes by 3.93 (A -> A2): **quadratic growth confirmed**.
- The throttle gives ~4x relief (`interval=1` writes 45.9 MB vs 11.75 MB) but the shape stays
  quadratic.
- Single-write size = full state: in B every post-image retained checkpoint rewrites the
  full message list (~25 MB each, 6 copies total). This reproduces the production incident
  threads exactly (6089: 16.6 -> 21.1 -> 24.5 MB snapshots; 6093: ~25 MB x several).

### 1.3 Production distribution (read-only; as-of 2026-09-11 18:02-18:04 CST)

- DB total 2.44 GB. `checkpoint_blobs` 2.19 GB physical / 29,596 rows; `checkpoints` 7,169
  rows; `checkpoint_writes` 6,995 rows.
- By channel: `messages` 4,697 rows / 1.87 GB (**99.3% of blob bytes**); `memory` 5.7 MB;
  `capabilities` 5.5 MB; everything else kB-scale.
- Largest threads (blob bytes): 6093 = 75.1 MB (11 rows, max 25.0 MB); 6089 = 62.1 MB;
  2986 = 48.1 MB (433 rows, max 1.29 MB); 3880 = 45.0 MB; 6041 = 40.9 MB; 2985 = 39.0 MB.
  Threads are keyed by agent id; 6093/6089 are the recent screenshot-heavy recon agents,
  2986/3880/2985 the long-lived workhorses.
- Concentration: 3,593 threads total; 19 threads > 10 MB hold 30% of all bytes; top 15 hold
  28%; 3,295 threads are < 1 MB.
- Images: 263 blobs contain image payloads = 458 MB = **23.5% of all bytes**; the 45 image
  blobs >= 1 MiB alone are 326 MB. Because snapshots repeat, one 25 MB image is stored
  ~5-6 times across 6093's retained window.
- `messages` version count equals checkpoint count per active thread (2986: 53/53): every
  retained checkpoint rewrote the messages channel, as designed by the merge above.
- Growth: the 2026-08-27 baseline put the checkpoint footprint at ~1.4 GB; it is ~2.2 GB
  now, the delta being the September screenshot-heavy threads.

### 1.4 Retention and vacuum today

- Reaper (`services/events_maintenance/checkpoint_reaper.py`): every 60 s, every thread over
  `_KEEP = 3` checkpoints trims to 3; <= 64 productive trims per pass, rotated for fairness;
  `compact_boundary: true` rows are exempt. (This superseded the older Rule A/B notes; see
  the shared memory correction of 2026-09-11.)
- Compaction (`agent/hooks/compact.py`): stamps the boundary, then trims the frozen
  pre-compact history to `COMPACT_TRIM_KEEP = 1`. A compaction replaces the whole
  conversation with `[system prompt, summary]` — stored state shrinks massively.
- Vacuum (`services/events_maintenance/blob_vacuum.py`): plain `VACUUM (ANALYZE)` in the
  05:00-08:00 cluster-time window; never `VACUUM FULL` (ACCESS EXCLUSIVE, user-ruled out).
- Safety invariant (`shared/checkpoint_cleanup.py` docstring): keep-latest-K trimming is
  correct ONLY while every channel stores a self-contained full value. Delta channels
  violate it; the module already carries the warning.

### 1.5 Mitigation status — guard landed, then reverted (task #3139)

PR #2226 (task #3096 / #6094) added `AVA_CHECKPOINT_MAX_BLOB_BYTES` (default 16 MiB), attach-
image downscaling and per-agent overrides. The Trunk queue landed it at 2026-09-11T17:51:21Z
(squash 740627d06) — one second before the recall; the user thereupon ruled caps/downscaling
wrong for this problem and task #3139 reverted the whole PR (guard, downscaling, overrides,
tests). **None of it is in the codebase.**

Threads over 16 MiB today (max blob octet_length): **6093 = 25.7 MiB (live, writing),
6089 = 23.3 MiB (killed/dormant)**. Next: 6041 = 9.6 MiB (the 8-16 MiB watch band). With no
guard in place these threads stay writable; their writes remain slow and a cross-network
write can still stall. No mitigation is in place while the structural direction is
re-decided; Section 6 keeps the exits that do not depend on the guard.

## 2. Failure anatomy

Per-thread write volume over N super-steps with content growing linearly and retention every
`i` steps: `W ≈ N^2 * rho / (2 i)` where `rho` is bytes added per step — quadratic in
content, linear inverse in interval. The incident adds the cross-network amplifier: each
retained write ships the full state (tens of MB) from the runner to the WSL-hosted Postgres,
against a 1-minute `statement_timeout`; retries stack more full rewrites.

Two orthogonal levers follow: shrink the per-write size (T1: externalize payloads; T2: write
deltas) and bound how often full state is materialized (retention + snapshot policy).

## 3. Candidate storage models

### 3.1 A — per-message append (message rows + checkpoint references)

Each message becomes a row (`thread_id, seq, msg_id, blob, created_at`); a checkpoint row
stores the tail reference/range and small state. Writes touch only new messages; reads
resolve ranges and reconstruct.

- Write profile: new-message bytes only — no rewriting of unchanged messages, no repeated
  images. Read profile: latest state = base snapshot + range scan; historical reads need
  range-to-id resolution.
- Compression boundary: per message row (msgpack per message; TOAST compresses each).
- Reaper: keep-K must become window-based GC over message rows (delete messages outside the
  retained windows + compact-boundary segments), not just checkpoint rows.
- Fork: copy or share the message range (mechanically simple; a shared slice would need
  same-DB reference semantics).
- Impersonation: flush boundary stays self-contained if a snapshot (row range + small state)
  is materialized; otherwise the same force-snapshot discipline as delta.
- Trace/timeline: resolution by checkpoint id must map to the row range; direct SQL readers
  change.
- Assessment: effectively what DeltaChannel already implements (its `checkpoint_writes`
  rows are the append log, snapshots bound replay), but with message-level granularity and
  no beta flag. Building it custom means owning every read/write path — the largest custom
  surface of the three.

### 3.2 B — large-payload externalization (content-addressed, full-snapshot kept)

Channel values keep their current shape, but payloads over threshold `T` are extracted at
write time into a content-addressed store (`sha256` key, chunked rows), replaced by a small
reference object; reads expand references before handing values out. Full-snapshot
semantics, trim, fork, and traces all keep working unchanged; the heavy bytes stop being
rewritten and stop being duplicated.

- Write profile: per retained checkpoint = serialized text/state + one-time payload writes.
  The 25 MB image costs 25 MB once (chunks), not 6x25 MB; later snapshots carry ~100-byte refs.
- Read profile: expansion at the saver boundary; direct raw-SQL readers audited (Section 4.2).
- Compression boundary: per chunk (4 MiB), each TOAST-compressible; dedup by hash across
  threads (fork shares payloads for free).
- Reaper: two added rules — refs die with their blob versions (same trim statement family);
  CAS rows are GC'd when unreferenced, batched like blob deletes. Compact boundaries and
  keep-K semantics unchanged.
- Fork: today's `_copy_checkpoint_chain` copies chain + all blobs; refs copy with blobs and
  payloads are shared (same DB) — no new copy semantics.
- Impersonation: unchanged (flush still writes full state, just slimmer); reads expand.
- Trace/timeline: read paths expand transparently once they use the aware saver; the
  message-count reader that reads the blob header still sees a valid array header.
- Guard fit: excellent — the refused single 25 MiB blob becomes text + refs; chunk rows are
  capped independently (chunk_size < guard).
- Assessment: smallest conceptual delta on top of today's model, directly aimed at the
  incident class and at the existing over-limit threads (repair = externalize their payloads,
  after which they are writable under the guard again). Residual: the text part of a long
  thread still rewrites quadratically (much smaller constants) — that is T2's territory.

### 3.3 C — delta storage: upstream DeltaChannel vs custom BaseCheckpointSaver

Upstream (langgraph 1.2.4, `langgraph-checkpoint-postgres` 3.1.0): `DeltaChannel`
(`langgraph/channels/delta.py`) stores only a sentinel in checkpoint blobs for non-snapshot
steps; state reconstructs by replaying ancestor `checkpoint_writes` rows up to the nearest
snapshot (`_DeltaSnapshot` blob, msgpack EXT). Cadence: snapshot every `snapshot_frequency`
updates (default 1000) or `DELTA_MAX_SUPERSTEPS_SINCE_SNAPSHOT` super-steps (default 5000,
env-tunable). The Postgres saver implements the delta read path
(`get_delta_channel_history`/`aget_delta_channel_history`, paged ancestor walk). Message
reducer: `_messages_delta_reducer` exists but is marked experimental.

Measured on the same scratch harness (minimal StateGraph, `messages: Annotated[list,
DeltaChannel(...)]`, 120 super-steps, ~780 KB final, no wrapper):

| Config | blobs | writes (deltas) | checkpoints | total | vs current model |
|---|---|---|---|---|---|
| no snapshots forced | 14 B (sentinel row) | 767 KB (362 rows) | 85 KB | ~0.85 MB | ~13.8x less than 11.75 MB |
| snapshot_frequency = 25 | 1.57 MB (5 snapshot rows) | 767 KB | 85 KB | ~2.43 MB | ~4.8x less |
| read-back at latest | — | — | — | 5 ms (local) | 240 messages reconstructed |

Failure modes and interactions (required evidence for a go/no-go):

1. **Retention severs the chain, silently.** Blunt keep-latest-3 emulation (delete older
   checkpoints and their writes) on the delta thread: `state.messages` reconstructs
   **240 -> 4 messages**, no error raised. This is the same hazard the repo's
   `shared/checkpoint_cleanup.py` invariant and langgraph `BaseCheckpointSaver.prune`'s
   warning describe. Adopting delta requires T3 (force a snapshot at the surviving
   checkpoint before deleting ancestors, one of langgraph's documented safe options).
2. **Crash replay window.** Today the wrapper means a crash replays up to `interval - 1 = 3`
   super-steps. With delta the wrapper's `aput_writes` skipping cannot continue (skipped
   deltas are unrecoverable), so deltas persist per super-step and the replay window
   becomes langgraph-standard async (last persisted checkpoint, ~<= 1 super-step) — strictly
   better, but the wrapper must be retired for delta channels; its
   `_versions_with_current_blobs` merge becomes a no-op for them (delta channels have no
   value in `channel_values` outside snapshots).
3. **Fork.** `_copy_checkpoint_chain` copies the checkpoint chain and all blobs but NOT
   `checkpoint_writes`; a delta thread's reconstruction needs writes since the last
   snapshot. Fix: materialize a snapshot at the fork checkpoint (simplest), or copy writes
   since the last snapshot.
4. **Impersonation / recovery flushes.** `flush_checkpoint` (impersonation) and
   `db_recovery` should force a snapshot at the boundary so the handoff state is
   self-contained for external readers.
5. **Read paths.** Pregel's load is delta-aware already; `shared/checkpoint.py` readers
   (messages, message-count via raw blob header, compact-segment reads, trace reads) and the
   gateway timeline/state endpoints must use the delta-aware API; the message-count header
   reader survives T1 unchanged (the blob stays a msgpack array header) but NOT T2 — a
   delta version has no blob row, so it must switch to reconstruct-based counting.
6. **Beta + version coupling.** The channel is documented beta ("on-disk representation may
   change"); the repo pins langgraph 1.2.4 and the D dependency line (#6096, task #3099) may
   touch langgraph. Adopting it is a pinned-contract decision, not a drop-in.

A fully custom `BaseCheckpointSaver` could implement delta (or model A) without the beta
flag, at the cost of owning every read/write path (including the walk SQL the upstream saver
already ships, paged and indexed). Given the repo's "don't reinvent LangGraph" principle,
the recommendation treats a custom delta saver as a documented fallback, not the chosen path.
Note that T1's extraction layer needs a saver subclass regardless; it can be the same
subclass.

### 3.4 Comparison

| Axis | A per-message append | B externalization | C delta (upstream) | today |
|---|---|---|---|---|
| Per-retained-step write | new msgs only | text + refs (payload once) | delta rows (+ periodic snapshot) | full state |
| Thread-lifetime growth | linear | linear (payload) + quadratic text | ~linear | quadratic |
| Single-write size | small | small | delta size / snapshot size | full state (25 MB) |
| Read cost (latest) | range resolve | expand refs | replay since snapshot (bounded) | single blob |
| Read cost (historical) | range resolve | expand refs | ancestor walk since snapshot | single blob |
| Compact boundary | window GC | unchanged | force snapshot at boundary | unchanged |
| Reaper keep-3 | window-based GC (new) | refs GC ride trim | T3 force-snapshot required | current trim |
| Fork | copy/share range | payloads shared, refs copy | snapshot at fork point | copy chain + blobs |
| Impersonation flush | snapshot at flush | unchanged | snapshot at flush | unchanged |
| Trace/timeline | new resolution | expand via saver | delta-aware reads | current |
| Guard (16 MiB) fit | yes | yes (chunks) | deltas yes; snapshots can exceed | no (incident) |
| New dependency/beta | none | none | beta feature, version-pinned | — |
| Custom surface | largest (owns all paths) | medium (extraction/expansion + GC) | medium (adapters only) | zero |

### 3.5 Recommendation

1. **T1 (externalization) first.** It resolves the incident class and the guard-era
   over-limit threads, keeps every existing invariant, adds no framework dependency, and is
   the smallest safe conceptual change. It also composes: T2 later rides the same saver
   subclass.
2. **T2 (delta for `messages`) as a gated second step**, only with T3 landed, the wrapper
   retired for delta channels, the fork/impersonation snapshot fixes, and an explicit
   decision on the beta-API pin (coordinate D=#6096). Measured evidence supports the
   payoff (~5-14x), and the reaper hazard is understood and fixable.
3. **T3 unconditional iff T2** (and cheap insurance for any future delta use).
4. **Explicit non-goals:** no change to agent-visible content (refs are storage-layer);
   no message-level semantics change (tool outputs keep their shape after expansion); no
   frontend timeline redesign; no change to backup scope.


## 4. Target design — T1 (large-payload externalization) in detail

### 4.1 Content store

- `checkpoint_content(hash bytea, chunk_seq int, chunk bytea, total_size bigint, created_at)` —
  PK `(hash, chunk_seq)`, sha256-addressed, chunked (candidate chunk 4 MiB; config
  `AVA_CHECKPOINT_CONTENT_CHUNK_BYTES`). A payload is written once; all later snapshots
  reference it. Refs are thread-agnostic: forks and sibling agents share payloads for free.
- `checkpoint_content_refs(thread_id, checkpoint_ns, channel, version, hash, chunk_count)` —
  one row per (blob version, extracted payload), written in the same transaction as the blob
  row, deleted by the same trim statement family that deletes blob versions (add one arm to
  the existing CTE in `shared/checkpoint_cleanup.py`).
- GC: `checkpoint_content` rows with no refs row are unreferenced; delete in bounded batches
  from the trim/vacuum daemons (`services/events_maintenance/`), same posture as blob
  vacuum. Nothing else deletes content.
- Extraction rule (write time, before serialization): inside a channel value, `bytes` or
  `str` payloads >= `AVA_CHECKPOINT_EXTRACT_MIN_BYTES` (candidate default 256 KiB) are
  replaced by a typed reference object carrying `{hash, total_size, chunk_count}`. The
  stored bytes are exactly the original ones (lossless); expansion is byte-identical.
- Reference type must be in the checkpoint msgpack allowlist (`agent/state.py`), added on
  both writer and reader sides in the same release.

### 4.2 Saver layer and reader audit

- One shared saver subclass (extend `PooledPostgresSaver` or a new
  `shared/checkpoint_content_layer` class) used by BOTH the runner and every gateway reader
  (today `shared/checkpoint.py` constructs upstream `PostgresSaver` directly — switch those
  call sites).
- Write path: extract (4.1) between value assembly and `_dump_blobs`/`_dump_writes`, so the
  size guard sees the slim blobs. Extraction must be async-safe and must not block on the
  content store more than the existing write path does.
- Read path: expand after deserialization in the tuple-loading path; values without refs
  pass through unchanged (dual-read). Adjacent raw-SQL readers:
  - `shared/checkpoint.py::load_checkpoint_message_count` reads the blob header only — the
    array header stays valid; verify per message-shape change, keep or switch.
  - `load_checkpoint_messages_segment` / `_full` / `_by_trace` and
    `gateway/routers/{timeline,agents_state}.py` must use the aware class.
  - `ops/agent_spawn.py::_copy_checkpoint_chain` copies blobs as rows; refs copy with them,
    content shared — no change beyond tests.
- Failure posture: mirror the guard — a failed extraction/expansion is a loud error, never a
  silent drop; partial writes roll back with the enclosing transaction.
- Guard interplay: refs keep single blob rows small; apply the same cap to chunk rows.

### 4.3 T2 sketch (gated; full design when picked up)

`messages: Annotated[list, DeltaChannel(reducer, snapshot_frequency=X)]` with
`_messages_delta_reducer` (or a repo-owned equivalent if the experimental reducer's gaps
matter); all other channels keep full snapshots. Migration note: upstream 4.2.0 / 3.1.2
make delta-history walks recognize plain-value seeds, so a hybrid thread (pre-delta full
snapshots + post-delta writes) stays reconstructible mid-migration without a forced
re-snapshot - re-verify on the frozen pins before relying on it. Required companions:
T3, wrapper retirement for the delta channel (deltas must persist every super-step; the
throttle's merge is a no-op there), fork snapshot materialization, impersonation/recovery
forced snapshots, reader audit, snapshot cadence tuned to bound ancestor walks (candidate:
much lower than the 1000 default; the 5000-superstep system bound stays). Every knob gets
config + tests, following this document's measured harness.

### 4.4 Interactions (T1; deltas noted where they change)

- Compact boundary: unchanged; the boundary checkpoint stays a full-snapshot record (its
  refs point at content that GC must keep — refs ride the surviving version).
- Reaper keep-3: unchanged for checkpoints; adds the refs arm + CAS GC. If T2 lands, T3
  applies here.
- Fork: unchanged mechanically; payloads shared; a large-image thread forks cheaply.
- Impersonation: flush/restore unchanged; reads expand.
- Trace/timeline: read paths keep working once they use the aware saver; trace resolution by
  `trace_id` returns expanded content as today.
- Backup/PITR: one more table to include in dumps/size accounting (`services/backup.py`);
  PITR unaffected (same WAL).
- Monitoring: keep the existing `checkpoint_blobs` high-water alert; add a CAS size/growth
  watch; extend events-maintenance reap telemetry.

## 5. Migration path (expand-contract)

Ordering principle (reader-first, writer-second; new writer only after old-code writers are
drained), mirroring the existing two-phase rollout discipline (workspace-6087
`bootstrap-manual-final.md`: old incumbents finish their turn and stop writing before new
code takes over; readers tolerate old and new shapes throughout):

1. **Step 0 — read-side code, no schema, no behavior.** Ship the aware saver with expansion
   + extraction behind flags (default off). Rollback: flags.
2. **Step 1 — expand.** Create `checkpoint_content` / `checkpoint_content_refs` +
   indexes; paired `.down.sql` drops them. No behavior change. `db/schema.sql` synced;
   `scripts/lint_migrations.py` clean.
3. **Step 2 — enable extraction for new writes** (cluster-level flag, only after the reader
   fleet is new). New checkpoint blobs carry refs; old-shape blobs keep working (dual-read).
   Rollback: flag off (Step 0's read side still expands whatever was written).
4. **Step 3 — backfill.** Batched job: for each blob version with payloads >= T, rewrite the
   blob in place to refs + CAS rows (idempotent, paced, restart-safe, per-thread bounded).
   Down path: re-inline from CAS (CAS retained through the rollback window). Gate: all
   readers new; the over-limit threads are the first batch (Section 6).
5. **Step 4 — contract (later).** Drop the re-inline down path after the rollback window;
   monitoring cleanup. Schema stays otherwise; nothing else to remove (additive design).

Takeover ordering for the writer: a thread may switch to ref-shape writes only when no
old-code writer can touch it — the existing drain semantics provide that point (old
incumbent finishes its turn; the n-step flush is the last old-shape write). No mixed-writer
window on the same thread; readers span both shapes the whole time.

Verification matrix (each with tests before landing): extract/expand round-trip (bytes
identical), dedup (same payload twice -> one CAS set), GC (trim deletes refs + orphan
content; survivor refs keep content), fork under refs, trim under refs, guard boundary
(ref-shape blob < limit; chunk < limit), backfill idempotency + interrupted resume, rollback
(re-inline), schema-current bidirectional check, and an e2e turn on a scratch cluster.

## 6. Transition period and the short-term mitigation boundary

Facts (Section 1.5): the guard no longer exists (reverted, task #3139); over-limit threads
write slowly rather than failing fast. Live today: **6093 (25.7 MiB, active)**;
**6089 (23.3 MiB, dormant)**; **6041 (9.6 MiB, watch band)**.

Exits for an over-limit thread (decision paths, in order of preference):

- **(a) Offline repair (durable; = targeted Step 3).** Externalize the oversized payloads in
  the thread's retained blob versions; the max blob drops and the thread stops shipping
  multi-megabyte rewrites. Lossless (expansion returns the same bytes; timeline renders the
  same content). Requires the reader-first sequencing; run per thread, batched.
- **(b) Stopgap limit raise — voided.** This path presupposed the reverted guard
  (task #3139); there is no cap in the codebase to raise.
- **(c) Fork-fresh fallback.** Spawn a successor with a handoff; the old thread stays frozen
  (cold history). Resumability is lost; last resort.
- **(d) Detection + triage.** Query (run read-only, low frequency; 16 MiB kept as a rounded
  watch line):
  `SELECT thread_id, max(octet_length(blob)) FROM checkpoint_blobs GROUP BY 1 HAVING
  max(octet_length(blob)) > 16*1024*1024;` — route the result to (a)/(c) by liveness and
  user value.
- **(e) Guard extension — voided.** It presupposed the reverted guard (task #3139); no
  cap/admit machinery is in the codebase.

Monsora-line benefit (explicit): the standing ruling parks the Monsora line on WSL until
large cross-network writes are addressed; after T1 (and further with T2) per-step bytes stop
being proportional to full state, so the machine-affinity constraint relaxes and the line can
return to company-mini/company-air under the normal placement rule.

## 7. Version sensitivity, risks, open questions

- **Version pin.** Conclusions are read against langgraph 1.2.4 /
  langgraph-checkpoint 4.1.1 / langgraph-checkpoint-postgres 3.1.0. The D dependency line
  (#6096, task #3099) targets langgraph 1.2.11 / checkpoint 4.2.0 / checkpoint-postgres
  3.1.2. The delta-related upstream notes between the pins are fixes with no explicit
  breaking declaration (1.2.5 empty-thread `updateState`; 1.2.7 snapshot overwrite +
  exit-mode UUIDs; 1.2.8 fresh-thread `updateState` now forces a snapshot - a behavior
  change to re-check; 1.2.9 `updateState` counters; 1.2.11 / 4.2.0 plain-value seed
  writes; 3.1.2 plain-value seed discovery when walking delta history). Re-run the scratch
  harness against the frozen pins before any T2 decision; T1 depends on no framework
  internals beyond the saver subclassing pattern already in use.
- **Beta API.** `DeltaChannel` is documented beta with an explicitly unstable on-disk
  contract; adoption is a pinned-contract decision (T2 only).
- **Migration transaction scoping.** CAS multi-row writes must follow the repo's PgBouncer
  write posture (2026-09-02 sweep) — one transaction per logical write, no autocommit
  fragments.
- **Measurement caveat.** Byte counts here use `octet_length` (raw); TOAST pzips
  compressible content, so `pg_column_size` undercounts — dashboards should pick one metric
  and stay consistent.
- **Open questions.** Extraction threshold T default; chunk size; refs expansion point
  (saver vs message construction); per-agent guard override; whether compact should
  externalize retained images rather than keep them inline (info-preservation ruling);
  snapshot cadence if T2 lands.

## Appendix A — harness and queries

- Scratch harness: local `initdb` cluster on port 55432 (never production), the real
  `AsyncPostgresSaver` + `build_checkpoint_serde()` + the real
  `_wrap_saver_writes_with_nstep_interval` imported from `agent/startup.py`; a minimal
  StateGraph with `DeltaChannel` for the delta runs. Scripts and raw per-call JSON live with
  the author (#6095); the numbers above are reproducible from them.
- Detection query for over-limit threads: Section 6(d). Distribution queries (top threads,
  channel mix, image share) were read-only one-shot aggregates against the runner's DB URL.

## Appendix B — selected production numbers (as-of 2026-09-11 18:03 CST)

- `checkpoint_blobs`: 2.19 GB physical, ~1.9 GB live bytes (sum pg_column_size); 29,596 rows; `messages` = 99.3%
  of bytes; images = 23.5% of bytes (263 blobs), of which 45 rows >= 1 MiB = 326 MB.
- Top threads: 6093 75.1 MB / 6089 62.1 MB; 19 threads > 10 MB = 30% of all bytes.
- DB total 2.44 GB; a 2026-08-27 baseline measured the checkpoint footprint at ~1.4 GB.
