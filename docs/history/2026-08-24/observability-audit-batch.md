# Observability audit batch

The alert schedule now reserves the five-minute group for low-cost, long-lived
signals: trace watermark and billing-quota detection join the two fast-route
latency tiers and turn-duration p95. Their existing alert windows remain
unchanged, while the grouping avoids evaluating their wider queries every
minute.

Native Grafana is now a converge-managed, pinned release with its own rendered
configuration, secret-reading launch script, owner-scoped launchd job, and
readiness check. The checkout stays the provisioning source. Tempo's query
base URL is a distinct host setting from its OTLP intake base URL so the
datasource and Prometheus scrape target select the correct endpoint.

Gateway latency events carry a route class derived from the rule exclusions;
slow-request alerts therefore select the fast class without duplicating route
matching policy. The turn-duration dashboard now reads the same Prometheus
histogram quantiles as its alert, and the resolution-labeled warning/error
tiles state their current all-event behavior until task #1468 supplies the
producer.
