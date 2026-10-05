# PG-backed inspect stats

The per-agent inspector's whole-life window now resolves through the durable
daily metrics/token ledgers, with the frozen `events` archive supplying the
historical duration distribution, lifecycle replay, and node-exit duration
totals. The retained Loki tail supplies only live raw-event edges.

The archive/Loki seam is intentionally not bridged: data between the archive
freeze on 2026-08-13 11:54 +08 and resumed Loki mirroring on 2026-08-16 03:10
is permanently unavailable. Reporting only recorded data is preferable to an
invented cumulative value.

New live inspect queries are split into clock-aligned windows no larger than
three hours and run concurrently through the existing Loki query budget. The
stats path consolidates counts and duration distribution per shard, so one
distribution feeds duration sum/min/max while the stitched archive/live
distribution supplies the window's percentiles.

## Gap-day live re-read

When the newest ledger day's UTC start is still within Loki retention, inspect
excludes that potentially stale row and rereads the entire day from the live
tail. A late write into a closed day is therefore neither lost nor double
counted, while older settled days remain ledger-served.
