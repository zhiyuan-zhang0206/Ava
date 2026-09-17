# No-observability clusters: refused reads and the mirror fallback

## The refusal is a policy state, not an outage

A gateway whose home is not the observability station and that has no explicit
`AVA_TELEMETRY_LOKI_URL` refuses observability reads outright (the read gate in
`gateway/_loki_transport.py`). The wire answer is a 503 `application/problem+json`
with `"code": "observability_read_unavailable"` — "set AVA_TELEMETRY_LOKI_URL and
provide its stack, or accept that this cluster has no observability".

- Affected reads include `/api/events` and `/api/agents/<id>/neighbors`.
- Retrying cannot clear it: it is configuration, not load. Do not treat it as a
  fault, and do not spend a retry budget on it.

## The fallback: the local event mirror

`logs/events-<UTC day>.jsonl` (`shared/telemetry`'s local copy of every event
this box emitted, 7-day retention) is a complete source on a single-box
cluster. Both self-evolution flows use it when the read is refused:

- **Daily scan** — `collect.py` raises `ObservabilityReadUnavailable` on the
  first refused reply. `daily_scan.py` then collects the window through
  `mirror_backfill.collect_from_mirror` (records plus the pre-filter counts the
  empty-window sentinel needs). The report carries
  `source: local event mirror (...)` in its stable tail, and a window whose
  mirror file(s) are missing forces exit 2 — never a silent partial dataset.
  On a fresh cluster the first window flags the pre-cluster day once; it
  self-resolves from the next run.
- **Weekly trigger** — `count_events` counts `[since, now]` from the same
  mirror (ts-filtered; files partition by append day, not by ts) instead of
  raising. The count is an upper bound (the emitter can duplicate rows) —
  fine for the trigger's coarse volume bands.
- **Manual, for any other failure** (e.g. dense-window 500s):
  `reference/mirror_backfill.py <days> [week]` writes `daily/<week>.jsonl`;
  a missing mirror day exits non-zero.

Every other failure path is unchanged: transient `/api/events` failures retry
within the budget (5 attempts, 5/10/20/40 s) and then fail loudly (rc=1 + wake).
