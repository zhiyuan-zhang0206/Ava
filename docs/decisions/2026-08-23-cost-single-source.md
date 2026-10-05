# Cost single source

`GET /api/stats/dashboard` now sums tokens and `cost_usd` from Loki
`llm_usage` events. The cost field is the producer's usage-time pricing
snapshot, so the sidebar and Grafana no longer disagree when the current model
registry changes.

The alternative — multiplying Prometheus token counters by current registry
rates at dashboard-read time — was removed because it rewrote historical spend.
Prometheus remains the fleet graph's token aggregate source; it is not a cost
reader for the dashboard.

Grafana's familiar `LLM cost (24h)` and `Tokens (24h)` cards keep their names
and use panel-local `now-24h` windows, while the dashboard default remains 6h
to bound general Loki query load. Daily, projection, model, and agent cost
panels read the same Loki snapshot field.

Update (delegator review pass 1): `llm_usage` is telemetry-only, so the status
card applies the same category filter as every Grafana LLM panel. Its four
windowed token/cost sums now have a 30-second per-window TTL cache, matching
the sidebar's 5-second polling cadence without caching turn or alert gauges.

Update (QA review pass): the sidebar polls every 30 seconds, so the token/cost
cache now lasts 60 seconds. Each of its four fields uses one full-window Loki
instant aggregate; only turn and alert reads retain their <=3-hour sharding.
Cost analysis now follows the dashboard-wide expanded-row convention.
