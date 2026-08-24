# Rollup-source JSONL replay

## Context

The durable token and activity ledger is normally aggregated from Loki, whose
168-hour retention also bounds how long a missed maintenance pass can be
recomputed. The local full-event JSONL mirror had the same seven-day lifetime,
so the stated recovery source disappeared at exactly the point it was needed.
Extending that full mirror to the 90-day repair window was rejected: log-heavy
clusters produce roughly 0.4–0.8 GB per day, making the extra local footprint
disproportionate to the ledger data being protected.

## Decision

Every emitter keeps the full mirror at seven days and additionally appends only
`llm_usage`, `turn_end`, and exec-family events to a rollup-source mirror. That
filtered file defaults to 90-day retention, configurable with
`AVA_EVENTS_JSONL_ROLLUP_RETENTION_DAYS`.

After each Loki rollup, events maintenance reconciles the last ledger day below
the current Loki floor and overwrite-upserts available filtered mirror days.
The below-floor watermark is deliberate: the Loki pass has already written
newer retained days, and a global maximum at that point would conceal the older
discontinuity. Missing files are unrecoverable and remain skipped. A present
file that aggregates no known-agent rows is a failed replay, never a successful
watermark repair. The same path is exposed as a dry-run and selected-day CLI so
operators do not need a separate manual aggregation procedure.
