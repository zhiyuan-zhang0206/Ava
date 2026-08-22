---
type: doc
title: Process-state observable metrics
description: Fixed-cardinality Ava process state collected by the shared OTLP metrics backend without per-transition events or request-path I/O.
tags: [shared, telemetry, otlp, observability, metrics]
---

# Process-state observable metrics

`shared.telemetry_otlp.register_observable_metric` adds process state to the
same OTel MeterProvider used by event-derived metrics. A service registers a
fixed callback at startup; the SDK metrics thread later collects it and adds
the standard `machine` and `process` dimensions. Registration never opens an
exporter. Callback/registration failures shed the observation and produce one
local `_NO_EMITTER` diagnostic, so the observer cannot recurse into Loki or
break the application path.

The Grafana gateway proxy is the first producer. Its immutable process-local
snapshot has exactly three `resource` values (`http`, `sse`, `websocket`) and
publishes:

- `ava_grafana_proxy_capacity_active` — current reservations (gauge);
- `ava_grafana_proxy_capacity_capacity` — configured slots (gauge);
- `ava_grafana_proxy_capacity_rejected` — cumulative fast rejects (observable
  counter; Prometheus exposes `_total`).

Reserve/release only replaces the snapshot under its own short lock. Metric
callbacks read it without the lock, perform no DB/Loki/network call, and emit
no transition event. This avoids a Grafana-query feedback loop. Reservations
never wait, so there is no wait histogram. Alert R17 uses the five-minute
increase in rejections grouped by `machine,resource`, with an additional
five-minute `for` window for sustained saturation.

Unlike collector-pulled [[infra-metrics.ava.okf.md]], these are produced by Ava
code and therefore obey `AVA_TELEMETRY_OTLP_ENABLED` and carry application
dimensions rather than `job=ava-infra` / `host`.

Parent: [[telemetry-otlp.ava.okf.md|OTLP export backend]].
