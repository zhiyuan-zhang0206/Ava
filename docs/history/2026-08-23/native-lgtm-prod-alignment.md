# Native LGTM production alignment

The 2026-08-23 production cutover made the native LGTM topology the source of
truth. Repository defaults and provisioning must match that running state so a
later converge or deployment cannot restore the former container-oriented
addresses.

Promtail is retired. Each machine's existing OTel Collector now tails session
stdout and orchestration logs through filelog receivers, derives
`service.name` from the file name, and persists read offsets separately from
exporter queues. This keeps the previous session-name query semantics without
another binary or a second positions store.

The expensive gateway-latency and turn-duration alerts moved to a five-minute
Grafana rule group while their `for` windows stayed unchanged. Native Grafana
uses host-loopback datasources and an absolute dashboard provisioning path
supplied through its runtime environment; Tempo is the remote WSL trace
backend.

The cutover also preserves the concurrent index-label rollout: the collector's
bounded event-label promotion remains in the logs pipeline, and native Loki
receives the same OTLP resource-index mapping as the rollback configuration.
The two collector transforms are independent and batching remains last.

The launcher is native-only: it does not touch the Docker daemon or compose,
and the retained compose stack is a manual rollback path. The repository keeps
the neutral host-loopback Tempo default; production selects remote Tempo with
a host-scope `AVA_TELEMETRY_TEMPO_ENDPOINT` override.
