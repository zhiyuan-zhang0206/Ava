# Three-way warning/error resolution split (task #1935)

## Decision

The stats dashboard's warning/error numbers and the Grafana resolution tiles
share one class arithmetic. `services/events_maintenance/resolution.py` now
exposes the window-agnostic split — `level_splits(counts, active)` turns any
window's per-class counts plus the active `event_dismissals` rows into
per-level `total / dismissed / net` triples — and the daemon's fixed six-hour
pass and the gateway's selected-window dashboard both run it. The daemon
additionally publishes `dismissed_warnings` / `dismissed_errors` gauges beside
the existing unresolved ones, so Grafana's visible trio (Warning/Error Loki
tiles, Dismissed tiles, Unresolved tiles) sums by construction.

## Consequences

- The dashboard's `warnings` / `errors` keep their raw-total meaning; the new
  `warnings_dismissed` / `warnings_net` / `errors_dismissed` / `errors_net`
  fields carry the split. The class query is the daemon's exact grouped LogQL
  (one query shape, two windows), so a class dismissed on one surface is
  subtracted identically on the other.
- The frontend stats card and the inspector panel render the trio in Chinese
  (total / resolved / remaining), with a positive all-clear state when
  net is zero.
- A reopened (burst) dismissal stops cancelling its class on both surfaces —
  both read the same active set.
- Per-agent `agent_id`-scoped dismissal rows remain without arithmetic effect
  everywhere (v1 API rejects them); the split reads class-wide rows only.
