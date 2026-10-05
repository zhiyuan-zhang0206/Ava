# WAL-G over pgBackRest: the five-question record for point-in-time recovery

## Context

The self-written PITR stack was deleted and point-in-time recovery was rebuilt on
WAL-G ([delete](2026-10-02-delete-the-self-written-pitr-stack.md),
[base](2026-10-02-walg-physical-backup.md)). The choice was made in the 2026-09-30
design and is live; this entry writes down the comparison that
[`conventions/technology-selection.md`](../conventions/technology-selection.md) asks for at a
gate, because the repository held none. The stack it replaced had never been compared with
any off-the-shelf tool: the 2026-08-29 foundation decision lists Drive,
a synchronous cloud command in `archive_command` and `pg_receivewal`, and none of WAL-G,
pgBackRest or Barman.

What the comparison below rests on is the 2026-09-30 design's paragraph on prior art
(documentation-level, with a sentence per tool) plus the scratch drill and the real-bucket
smoke run of WAL-G. pgBackRest itself was never run here. Where a claim below is only
documentation reading, it says so.

## Decision

Keep WAL-G as the point-in-time recovery mechanism, behind the thin layer already built
(pinned binary, WAL-G's own configuration file passed through, a state probe, retention
invariants, a weekly restore drill). The five questions:

1. **Boundary.** WAL-G owns shipping WAL segments, base and incremental backups, the object
   layout and its encryption, retention deletes, and fetching for restore. Ava owns what
   WAL-G does not: when archiving is switched on (Postgres launch arguments), whether it is
   healthy (the probe), whether a retention delete is safe (dry-run plus structural
   invariants before `--confirm`), and whether a restore actually works (the drill).
   Whether the key survives is the operator's, by escrow.
2. **Prior art.** Postgres point-in-time recovery is a solved problem with three known
   open-source answers: WAL-G, pgBackRest, Barman. Using one is rung 3 of the ladder. The
   comparison that decided it, from the object store in use (Aliyun OSS):
   - WAL-G has a native OSS backend (`WALG_OSS_PREFIX`, region, endpoint), so it needs no
     adapter of ours. pgBackRest reaches OSS only through an S3-compatible endpoint, whose V4
     authentication might need separate enabling on the provider's side (documentation
     reading; not tried). Barman does not simplify OSS access.
   - pgBackRest is the more integrated tool (built-in verification, `expire`, restore by
     target, a spool-based asynchronous archive). That is a real advantage and the reason it
     is the named exit below.
   - WAL-G was proved against the real store before any commitment: a full-plus-incremental
     chain restored to a chosen time, the whole WAL history replayed from the oldest full
     backup, a retention delete survived, results identical to the source; then a real-bucket
     smoke run of check, backup, fetch, restore, drill and retention. pgBackRest got no such run.
3. **Simplest option that works.** One binary, one 0600 configuration file in the tool's
   native format, one `archive_command`, one daily run. No resident process, no adapter, no
   translation layer, no private format.
4. **Failure prevented, and what we now handle ourselves.** Prevented: a recovery that
   depends on this repository's own reader of its own object format. Handled by us because
   the tool leaves them open (all seen in the drill or smoke run):
   - `wal-verify` exits 0 while reporting a gap, and a gap within the last upload-concurrency
     window is only a warning; the drill and the archiver probe cover that window.
   - `backup-fetch` brings back the source's `archive_mode=on`; a restored instance must start
     with archiving forced off or it writes into the live chain. Ava keeps archive settings
     out of the data directory for this reason.
   - A lost or replaced encryption key makes every object unreadable; the key is pinned by
     fingerprint and escrow is a precondition of enabling.
   - Upload bandwidth (about 2.2 MiB/s) is the real limit; archiving and base backups share it.
   - A stuck or slow archive command stalls Postgres shutdown while segments are queued.
   - The tool is pinned for Linux x86_64 only.
5. **Limit signal and exit.** See the next section.

## Stop-loss: when to switch

Qualitative triggers; any one is a reason to reopen this entry, none is a number:

- A **second gap that only our own code can fill**, beyond the layer that exists today
  (probe, retention invariants, restore drill). The first such gap is the `wal-verify`
  blind spot; a further one means we are rebuilding what the other tool ships.
- The **upkeep of our layer costs more than replacing the tool**: the layer grows its own
  state, its own storage adapter, or its own translation of the tool's settings.
- The **same class of failure recurs** (an archive that silently stops, a retention delete that
  removes something restorable, a drill that passes while a restore would fail).
- The tool's **maintenance or object-store support degrades**, or Ava needs a platform
  beyond Linux x86_64 or a store the tool does not speak.
- pgBackRest's S3 path to OSS is **shown to work** (a scratch drill, not a reading of the
  docs) and its verification and expiry would delete code of ours.

Exit: pgBackRest, the only other rung-3 option. The cost is a new bucket prefix (a new
generation: two tools cannot share a chain) and a rewritten thin layer; the daily logical
dump stays the recovery point below the archive throughout, so the switch never leaves the
cluster without one.

## Alternatives rejected

- **pgBackRest.** Not rejected on merit: it lost on the OSS path (an S3-compatible endpoint
  with an authentication question, unverified) and on having no proof against our store.
  It stays the named exit.
- **Barman.** No advantage on OSS access.
- **Keeping the self-written stack.** See the delete decision: a private format only this
  repository can read, untested against anything but itself.
- **Finishing the stack's activation.** Its design was already what WAL-G is.

## Consequences

- The comparison is documentation-level for pgBackRest; if the exit is ever considered, the
  first step is a scratch drill of it against the same store, not this entry.
- The weekly restore drill is the guard that makes "WAL-G works here" a recurring fact
  rather than a one-time drill result.
- Revisit the claimed benefit once after it has run for a while: the restore drill's history
  and the probe's alerts are the evidence, not this entry.
