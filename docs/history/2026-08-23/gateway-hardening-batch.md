# Gateway hardening batch

## Decision

The gateway's recovery and interactive read paths now reserve bounded resources
instead of competing indefinitely with dashboard traffic. Health and pause
posture reads use a small control-plane pool; watchdog recovery requires two
consecutive failed gateway probes. The frozen `events` archive boundary is
loaded once per process, because the archive receives no post-cutover writes
and its unindexed maximum-timestamp scan is otherwise expensive.

All interactive Loki readers now carry explicit timeouts, with inspector
lifecycle data isolated into a five-minute, per-agent single-flight cache. It
remains a temporary guardrail for the legacy stream while retention removes
that slice after 2026-08-30 19:00 UTC; no ingest-time lifecycle accumulator was
introduced.

Prometheus joins the existing fair admission discipline, and gateway endpoints
translate local upstream saturation into a single retriable 503 contract.
Fleet graph, dashboard, Grafana proxy, page proxy validation, and idempotency
followers have bounded work and cache behavior appropriate to their request
paths. The index-label cutover itself remains owned by its separate work item.

## Verification

Focused gateway and shared regression suites cover the new bounds, cache
single-flight behavior, backend responses, and schema contracts. The legacy
Loki module retains its pre-existing cutover-window expectations, which are
tracked outside this batch.

## Update — P0-2 gateway respawn self-heal

The gateway's two-consecutive-failure threshold deliberately respawns during a
sustained DB outage about every two minutes, accepting loss of its stats cache.
PgBouncer pool poisoning has a real precedent, so respawn is the clean reconnect
path; changing this trade-off requires a fresh decision.
