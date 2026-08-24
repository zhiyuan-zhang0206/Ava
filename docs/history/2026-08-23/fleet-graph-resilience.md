# Fleet graph resilience

The fleet graph now separates a fresh full response from a degraded response.
Each successful refresh retains a parameter-specific last-good graph in Redis
for seven days, alongside the existing 30-second poll cache. The live Loki tail
uses an eight-second per-request HTTP deadline.

On Loki or Prometheus failure, the endpoint serves that last-good graph with
`stale=True`; without one, it preserves the PG node identities and returns no
edges. A canceled PG query has no fresh node set, so it uses last-good data when
present and otherwise retains the existing empty stale response. Degraded
responses never refresh the short poll cache, ensuring the next poll retries
the source reads instead of extending a failure.

## Follow-up: Prometheus deadline

The fleet graph's Prometheus aggregates now use the same eight-second
per-call HTTP deadline as its Loki edge query. Other Prometheus consumers
continue using the shared client's default timeout.

## Follow-up: polling and edge scale

The whole-response cache now has a 60-second lifetime while the frontend
continues reconciling every 30 seconds. This keeps UI polling responsive and
makes alternating polls Redis hits, limiting expensive source reads to once per
minute; SSE invalidation still carries lifecycle changes promptly. Message-edge
opacity now scales across each graph's own weight range rather than raw weight
values.

> Superseded in part by [Fleet graph freshness semantics](../2026-08-24/fleet-graph-freshness-semantics.md): a heartbeat-stale but successful graph remains cacheable and reports telemetry health separately from data freshness.
