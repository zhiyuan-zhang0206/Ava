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
