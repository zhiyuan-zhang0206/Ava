# Checkpoint storage rebuild — delta-channel model + cutover plan

Date: 2026-09-11 (revised 2026-09-14)
Author: #6095 (checkpoint-storage-design, tasks #3097/#3181)
Requester: #405 (user ruling 2026-09-11 17:53; direction re-decided 2026-09-12 23:19)
Status: **plan — final direction: delta channel + keep-everything (Sections 3.5, 5)**.
The four implementation PRs (#2313 retention exemptions; #2322 wrapper retirement; #2318
guard fast path; #2321 read-compat layer) are merged and deployed cluster-wide
(2026-09-13, 5/5 @ `30df11a83`); the write switch awaits its user-supervised window.
Code read against main@`c7e69429b`; production numbers are read-only, as-of
2026-09-11 18:02-18:04 CST (live table — snapshots, not constants).

## 0. Summary

The 2026-09-11 incident: an agent attached seven inlined screenshots (1-2 MB each) to its
conversation; every retained checkpoint re-serialized the whole message list, so each write
grew to ~25 MB, cross-network writes hit the 1-minute `statement_timeout`, and the turn
stalled permanently. The mechanism is general: **every retained checkpoint rewrites every
state channel in full**, so cumulative bytes written over a thread's life grow quadratically
with its content, and the single-write size equals the full state size.

**The direction (re-decided 2026-09-12, user ruling 23:19): move the `messages` channel to
append-style delta storage, keep everything, and fold at read time.**

- **Delta model.** `messages` becomes a `DeltaChannel(guarded_delta_reducer, ...)`: each
  super-step appends only new messages to the delta log (`checkpoint_writes`); state
  reconstructs by folding the log back to the nearest snapshot. Measured write volume:
  ~13.8x less than the current model when no snapshots are forced, ~4.8x with periodic
  snapshots (Section 3.3). Every other channel keeps the full-snapshot model (Section 1.1
  for the write mechanics).
- **Keep everything (R1-R4).** Nothing is deleted — tail-replaced old copies,
  compact-boundary snapshots, and the full delta log are all retained. The target state has
  no deletion path; cleanup may only append new snapshots/baselines (Section 1.4).
- **Read model — fold at read time.** A read-compat layer (`shared/delta_read_compat.py`
  plus saver-level injection) folds delta threads transparently, so every reader keeps
  working across the transition (PR #2321, deployed to every machine).
- **Cutover — reader-first and gated.** The read-compat layer is live everywhere before the
  write model switches; machines switch in batches under a user-supervised window, each
  batch verified against content fingerprints, with a materialize-and-rollback plan ready
  (Section 5).
- **Rejected alternatives (Section 3.5):** T1 payload externalization; size caps and image
  downscaling; periodic materialization. The T1 analysis that still matters is kept
  (Section 3.2); its detail chapter was removed (Section 4).

Rollout status (2026-09-13): implementation merged and deployed (`30df11a83`); the
pre-cutover drill gate ran green in the deployed shape (D1-D6 matrix, crash replay,
production-clone fingerprint subset — content fingerprints are the acceptance metric, not
counts or write volume). The write switch runs in the next user-supervised window.

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
  - A delta-bearing thread (checkpoint metadata `counters_since_delta_snapshot`, or a
    `_DeltaSnapshot` value in `channel_values`) retires the throttle: every super-step and
    write batch persists exactly as upstream produced them (original configs, no version
    merge). Delta reconstruction replays `checkpoint_writes`, so a skipped batch is
    unrecoverable and re-homing one scrambles the replay order — landed for delta threads
    (PR #2322; the wrapper-retirement companion this design required).
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

### 1.4 Retention and cleanup — history, the stop, and the final rules

**History (until 2026-09-12).** Three paths touched old rows: the reaper
(`services/events_maintenance/checkpoint_reaper.py`) trimmed every thread over `_KEEP = 3`
checkpoints down to 3 (60 s cadence, <= 64 productive trims per pass, rotated for fairness;
`compact_boundary: true` rows exempt); compaction (`agent/hooks/compact.py`) stamped the
boundary and trimmed the frozen pre-compact history to `COMPACT_TRIM_KEEP = 1`; and
`blob_vacuum.py` ran plain `VACUUM (ANALYZE)` in the 05:00-08:00 window (never `VACUUM FULL`;
user-ruled out).

**The stop (landed).** User ruling 2026-09-12 (task #3183): the reaper is stopped — disabled
in config (WSL `.env` false since 2026-09-12 22:46; verified no trims over the following
hours; 127 threads were over 3 checkpoints at the stop). PR #2313 then removed the compact
trim (`keep=1` deletion), made the reaper default off, and added the explicit delta-thread
exemption to the trim predicate (`_TRIM_SQL.delta_thread`: `counters_since_delta_snapshot`
metadata or a `_DeltaSnapshot` value), with a snapshot-point regression test.

**The rules (final).**
- **R1 — delete nothing.** `checkpoints` / `checkpoint_writes` / `checkpoint_blobs` rows are
  all retained; the target state has no deletion path. "Zero deletions" is an explicit
  property (predicate + tests), not a probabilistic accident.
- **R2 — the delta log is an append-only baseline.** Reorganization may only append new
  snapshots/baselines; it must never delete or rewrite existing entries.
- **R3 — growth is an engineering-side concern.** Keep PG compression (TOAST); levers are
  the snapshot cadence `K`, row/batch overheads, and an optional cold read-only export (an
  extra copy, never a replacement). Under evaluation for the switch: one small snapshot
  forced at the compact boundary (state is small right after compaction — caps the replay
  window, helps boundary-segment reads).
- **R4 — the boundary anchor stays.** The compact-boundary checkpoint stays as the segment
  anchor (no deletion at boundaries).

**Active deletion paths today.** The only deletion machinery is `_TRIM_SQL`
(`shared/checkpoint_cleanup.py`), reachable from the reaper and compaction and gated on
both (reaper default off; delta threads exempt). `blob_vacuum` is not deletion (dead-tuple
reclaim). `prune()` / `delete_thread` have zero non-test callers.

The module's safety invariant still stands as the reason for the exemption: keep-latest-K
trimming is correct only while every channel stores a self-contained full value — delta
channels violate that, which is exactly why they carry the explicit predicate above.

### 1.5 Mitigation status — the guard was reverted; the mutation guard meets delta

PR #2226 (task #3096 / #6094) added `AVA_CHECKPOINT_MAX_BLOB_BYTES` (default 16 MiB),
attach-image downscaling and per-agent overrides. The Trunk queue landed it at
2026-09-11T17:51:21Z (squash 740627d06) — one second before the recall; the user thereupon
ruled caps/downscaling wrong for this problem and task #3139 reverted the whole PR (guard,
downscaling, overrides, tests). **None of it is in the codebase**; the replacement is the
structural change this document describes — not a cap.

The mutation guard itself stays as the in-flight safety net — now integrated with the delta
channel (task #3187; the guard x delta reducer landed in main as PR #2286, `94e5f4fd1`):
the three legal message-list changes (clean rebuild / tail append / modify-last) are
enforced on delta writes too, so the delta log's invariants are guarded rather than assumed.

Threads over 16 MiB at the 9/11 read (max blob `octet_length`): **6093 = 25.7 MiB (live),
6089 = 23.3 MiB (dormant)**, 6041 = 9.6 MiB (watch band). With no cap they stay writable —
slowly, across the network — until the write switch moves `messages` to delta
(Section 6).

## 2. Failure anatomy

Per-thread write volume over N super-steps with content growing linearly and retention every
`i` steps: `W ≈ N^2 * rho / (2 i)` where `rho` is bytes added per step — quadratic in
content, linear inverse in interval. The incident adds the cross-network amplifier: each
retained write ships the full state (tens of MB) from the runner to the WSL-hosted Postgres,
against a 1-minute `statement_timeout`; retries stack more full rewrites.

Two orthogonal levers follow: shrink the per-write size (write deltas instead of the full
state) and bound how often full state is materialized (snapshot policy). Payload
externalization (T1) was evaluated as a third lever and rejected (Section 3.5).

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
- Read profile: expansion at the saver boundary; direct raw-SQL readers would need the
  same audit as any shape change (see the reader coverage list in Section 5).
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
  thread still rewrites quadratically (much smaller constants) — that is the delta model's territory.

### 3.3 C — delta storage: upstream DeltaChannel vs custom BaseCheckpointSaver

Upstream (as first analyzed on langgraph 1.2.4 / `langgraph-checkpoint-postgres` 3.1.0;
the pinned contract is Section 7): `DeltaChannel`
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
   warning describe. Resolution under this design: deletion is disabled outright (R1-R4,
   Section 1.4) — ancestor chains are never severed, so no retention snapshot-forcing step
   (the earlier T3) is needed — and the trim predicate keeps an explicit delta-thread
   exemption so any re-enabled cleanup cannot eat a delta chain.
2. **Crash replay window.** Under the throttle a crash replays up to `interval - 1 = 3`
   super-steps. With delta the wrapper's `aput_writes` skipping cannot continue (skipped
   deltas are unrecoverable), so deltas persist per super-step and the replay window
   becomes the in-flight super-step — strictly better. **Landed (PR #2322):** the wrapper
   retires entirely for delta threads; its `_versions_with_current_blobs` merge is a no-op
   there (delta channels have no value in `channel_values` outside snapshots).
3. **Fork.** `_copy_checkpoint_chain` historically copied the checkpoint chain and all
   blobs but NOT `checkpoint_writes`; a delta thread's reconstruction needs the writes since
   the last snapshot. **Landed (PR #2321):** the copy now carries the writes chain as well
   (same `task_id` / `idx` / namespace), so a forked delta thread reconstructs directly.
4. **Impersonation / recovery flushes.** `flush_checkpoint` (impersonation) and
   `db_recovery` boundaries must stay readable for external readers; the read side
   reconstructs at mount time (`load_snapshot`, PR #2321), and forcing a snapshot at the
   boundary remains available where a self-contained record is preferred.
5. **Read paths.** Pregel's load is delta-aware already; `shared/checkpoint.py` readers
   (messages, message-count via raw blob header, compact-segment reads, trace reads) and the
   gateway timeline/state endpoints must use the delta-aware API. **Landed (PR #2321):** the
   compat layer covers all of them; the message-count reader falls back to reconstruct-based
   counting when a delta version has no blob row (Section 5 lists the coverage).
6. **Beta + version coupling.** The channel is documented beta ("on-disk representation may
   change"). Adoption is a pinned-contract decision, not a drop-in: the stack is now pinned
   at langgraph 1.2.11 / langgraph-checkpoint 4.2.0 / langgraph-checkpoint-postgres 3.1.2
   (delivered by the D dependency line, #6096), and that pin is the on-disk contract
   (Section 7).

A fully custom `BaseCheckpointSaver` could implement delta (or model A) without the beta
flag, at the cost of owning every read/write path (including the walk SQL the upstream saver
already ships, paged and indexed). Given the repo's "don't reinvent LangGraph" principle,
the decision treats a custom delta saver as a documented fallback, not the chosen path.

### 3.4 Comparison

| Axis | A per-message append | B externalization | C delta (upstream) | today |
|---|---|---|---|---|
| Per-retained-step write | new msgs only | text + refs (payload once) | delta rows (+ periodic snapshot) | full state |
| Thread-lifetime growth | linear | linear (payload) + quadratic text | ~linear | quadratic |
| Single-write size | small | small | delta size / snapshot size | full state (25 MB) |
| Read cost (latest) | range resolve | expand refs | replay since snapshot (bounded) | single blob |
| Read cost (historical) | range resolve | expand refs | ancestor walk since snapshot | single blob |
| Compact boundary | window GC | unchanged | boundary anchor retained (optional small snapshot) | unchanged |
| Reaper keep-3 | window-based GC (new) | refs GC ride trim | not applicable — no deletions (R1-R4) | trim now stopped (Section 1.4) |
| Fork | copy/share range | payloads shared, refs copy | snapshot at fork point | copy chain + blobs |
| Impersonation flush | snapshot at flush | unchanged | snapshot at flush | unchanged |
| Trace/timeline | new resolution | expand via saver | delta-aware reads | current |
| Guard (16 MiB) fit | yes | yes (chunks) | deltas yes; snapshots can exceed | no (incident) |
| New dependency/beta | none | none | beta feature, version-pinned | — |
| Custom surface | largest (owns all paths) | medium (extraction/expansion + GC) | medium (adapters only) | zero |

### 3.5 Decision record (2026-09-12 re-decision; revised 2026-09-14)

**Chosen: model C — upstream `DeltaChannel` for `messages`** (on the pinned stack,
Section 7), combined with keep-everything retention (R1-R4, Section 1.4) and read-time
folding as the read model.

**Rejected:**
- **T1 — large-payload externalization.** Rejected in the 2026-09-12 re-decision: it keeps
  the full-rewrite write model (and its quadratic growth) and only shrinks its constants,
  while adding a content store, reference plumbing and a GC surface the chosen model does
  not need. The model analysis is retained in Section 3.2; the detailed design chapter is
  removed (Section 4 keeps the record).
- **Size caps / image downscaling.** User-ruled out (task #3139; Section 1.5).
- **Periodic materialization.** Rejected as a standing mechanism; one-shot materialization
  remains a cutover / rollback tool (Section 5).
- **A fully custom `BaseCheckpointSaver`.** Not chosen (the repo's "don't reinvent
  LangGraph" principle); retained as a documented fallback (Section 3.3).

Retention snapshot-forcing (the earlier T3) is moot under R1-R4: with no deletions, no
ancestor chain is ever severed.

Explicit non-goals: no change to agent-visible content; no message-level semantics change;
no frontend timeline redesign; no change to backup scope.

## 4. Rejected: T1 large-payload externalization (detail removed)

T1 stood here as the recommended first step in the 2026-09-11 draft; the 2026-09-12
re-decision chose the delta model instead (Section 3.5). The detailed design — content-store
and refs schema, extraction rules, saver-layer audit, expand-contract backfill — has been
removed. Section 3.2 keeps the model analysis; nothing from T1 ships.

## 5. Cutover plan — read-compat first, then the write switch

No schema change: the delta model writes the same three tables through the same saver —
`checkpoint_writes` carries the append log, `checkpoint_blobs` holds snapshots. The
migration is a code-and-configuration rollout, ordered reader-first, reusing the drain
semantics of the two-phase upgrade (old incumbents finish their turn; the n-step flush is
the last old-shape write; no mixed writers on one thread; readers span both shapes
throughout):

1. **Read-compat layer (PR #2321, merged and deployed).** `shared/delta_read_compat.py` plus
   saver-level injection fold delta threads at read time. Coverage: pool saver
   `get_tuple`/`aget_tuple` (folded values injected); `shared/checkpoint.py` readers
   (messages / count with reconstruct fallback / segment / full / by-trace);
   `ava/_external_state.load_snapshot`; fork chain copy (`_copy_checkpoint_chain`, writes
   chain included); `agent/startup.py` inbound reconciliation; `scripts/restore_drill.py`;
   the self-evolution recorder. Vanilla data passes through unchanged — verified inert on
   real production read paths (production-clone subset, wrapped == native x5). Deployed to
   every machine 2026-09-13 (`30df11a83`).
2. **Wrapper retirement (PR #2322, merged and deployed).** Delta threads bypass the N-step
   throttle; vanilla threads keep it bit-for-bit.
3. **Drill gate (green in the deployed shape).** Behavioral matrix + crash replay +
   production-clone fingerprint subset; acceptance = content fingerprints (count +
   first/last message ids + sha256 of the serialized messages), not counts or write volume.
   Evidence lives with the author (#6095).
4. **The write switch (user-supervised window).** `messages` becomes the delta channel. Per
   the cutover execution card: low-activity machines first, 30-60 min observation, then the
   rest, WSL (database host) last; every batch compares content fingerprints before and
   after; any mismatch, read failure, or content inequality pauses the batch and triggers
   rollback.
5. **Observation >= 24 h, then the close-out report.**

**Rollback.** Revert the code pin to the read-compat version — reads stay safe immediately
(that version reconstructs both shapes). For any active thread the new model wrote, run the
one-shot materializer: write one plain full-snapshot checkpoint (version monotonic,
parent = latest, same serde, append-only) so pre-compat code reads the current state again.
Verified in the drills (materialize + rollback flow; the x6 experiment: 24 -> 26 messages
across both read paths). Honest boundary: materialization restores *current-state
readability*; historical segment reads still need the fold from the code that wrote them.

Compatibility evidence (on the frozen pins): vanilla -> delta continuation reads and writes
correctly (~110 steps); delta -> vanilla direct read returns empty (silent) — the case the
materializer addresses; hybrid threads (pre-delta full snapshots + post-delta writes) stay
reconstructible via plain-value seed discovery (re-verified on the pins).

## 6. Transition period and the mitigation boundary

**Today (before the write switch).** No caps exist; over-limit threads write slowly rather
than failing fast, and nothing is deleted (R1-R4). The incident class disappears when
per-step bytes stop being proportional to full state. Until then, disk growth under the
current model with deletions stopped measures about +0.75 GB physical per 30 h; disk
headroom, alert thresholds and an owner are tracked on the cutover checklist.

**Housekeeping boundary — archive before any delete, and never delete the archive.** The
target state has no deletion path. Should a disk emergency ever force one, the only
acceptable form is archive-before-delete: export the doomed rows first (gz NDJSON with a
manifest; the #3182 design remains the reference), fsync and verify, then delete only the
verified-dumped set; any such dump is itself never deleted. Note the 2026-09-12 re-decision
re-cast "archive" as an optional read-only export (an extra copy, never a deletion
mechanism), and the scheduled archive-then-delete rework (#3182) is superseded as a task.

**Over-limit threads until the switch.** 6093 (25.7 MiB, active), 6089 (23.3 MiB, dormant),
6041 (9.6 MiB, watch band). Handling: keep writing (no cap), watch via the detection query
below; a fork-fresh handoff stays the last-resort exit for a thread that becomes unwritable.

- Detection (read-only, low frequency; 16 MiB as the rounded watch line):
  `SELECT thread_id, max(octet_length(blob)) FROM checkpoint_blobs GROUP BY 1 HAVING
  max(octet_length(blob)) > 16*1024*1024;` — route by liveness and user value.
- Fork-fresh fallback: spawn a successor with a handoff; the old thread stays frozen (cold
  history). Resumability is lost; last resort.

(The previous revision's exits (a) offline repair (= T1), (b) limit raise and (e) guard
extension are void: T1 is rejected and the guard was reverted.)

Monsora-line constraint: the standing ruling parked the Monsora line on WSL until large
cross-network writes are addressed; once per-step bytes move off full-state rewrites,
re-evaluate the placement under the normal rule.

## 7. Version sensitivity, risks, open questions

- **Version pin (the contract).** The stack is pinned at langgraph 1.2.11 /
  langgraph-checkpoint 4.2.0 / langgraph-checkpoint-postgres 3.1.2 (delivered via the D
  dependency line, #6096; deployed 2026-09-13). Every conclusion in this revision, the
  x-series experiments, and the pre-cutover drill suite were read/run on these pins. The
  delta on-disk representation is documented beta; the pin is our contract — a version move
  re-runs the drill gate before deployment.
- **Beta API.** `DeltaChannel` is documented beta with an explicitly unstable on-disk
  contract. Mitigations in place: pinned version, the read-compat layer as defense in
  depth, the materialize-and-rollback plan (Section 5), the drill gate, and an independent
  adversarial review (#6143).
- **Mixed-version window.** During the transition mixed shapes exist cluster-wide. Hard
  rules: every machine runs the read-compat layer before the write switch; readers tolerate
  old and new shapes throughout; no mixed writers on a single thread (drain semantics);
  per-batch content fingerprints catch silent mismatch — the failure mode here is silent by
  nature (Section 3.3).
- **Write batching.** Delta writes persist every super-step; the in-flight batch is the
  crash-replay bound on delta threads (PR #2322). The throttle stays bit-for-bit for
  vanilla threads.
- **Fork / impersonation.** Fork copies the writes chain; impersonation mounts through
  `load_snapshot` (both PR #2321); the refresh path (`flush_checkpoint`) can force a
  boundary snapshot where a self-contained record is preferred.
- **Measurement caveat.** Byte counts here use `octet_length` (raw); TOAST pzips
  compressible content, so `pg_column_size` undercounts — dashboards pick one metric and
  stay consistent.
- **Open questions.** Snapshot cadence `K` (calibrate after the guard fast path with the
  guard-inclusive, large-payload benchmark; keep it configurable through the observation
  window); the optional compact-boundary small snapshot (R3; decide before the write
  switch); scope of the read-path performance gate during the observation window.

## Appendix A — harness and queries

- Scratch harness: local `initdb` cluster on port 55432 (never production), the real
  `AsyncPostgresSaver` + `build_checkpoint_serde()` + the real
  `_wrap_saver_writes_with_nstep_interval` imported from `agent/startup.py`; a minimal
  StateGraph with `DeltaChannel` for the delta runs. Scripts and raw per-call JSON live with
  the author (#6095), beside the pre-cutover drill suite (behavioral matrix, crash replay,
  production-clone subset + closure check) and its raw outputs; the numbers above are
  reproducible from them.
- Detection query for over-limit threads: Section 6. Distribution queries (top threads,
  channel mix, image share) were read-only one-shot aggregates against the runner's DB URL.

## Appendix B — selected production numbers (as-of 2026-09-11 18:03 CST)

- `checkpoint_blobs`: 2.19 GB physical, ~1.9 GB live bytes (sum pg_column_size); 29,596 rows; `messages` = 99.3%
  of bytes; images = 23.5% of bytes (263 blobs), of which 45 rows >= 1 MiB = 326 MB.
- Top threads: 6093 75.1 MB / 6089 62.1 MB; 19 threads > 10 MB = 30% of all bytes.
- DB total 2.44 GB; a 2026-08-27 baseline measured the checkpoint footprint at ~1.4 GB.
- (Reference snapshot, kept as the design-time read; the Section 6 detection query
  reproduces the current view.)
