---
type: doc
title: "Infrastructure metrics — the sidecar's own scrapes"
description: "The traditional SRE layer (issue #46): each running OTel Collector scrapes host metrics and its own queue/drop/uptime metrics; the marked gateway also scrapes its usable data-plane receivers. The dedicated `metrics/infra` pipeline attaches `host` and lands in central Prometheus without extra exporter binaries."
tags:
- shared
- telemetry
- otlp
- observability
- metrics
- infrastructure
---

# Infrastructure metrics — the sidecar's own scrapes

Every other metric in this subtree is *pushed* by an Ava process. This one is
**pulled by the collector**: `deploy/otel-collector/otel-collector.yaml`
declares receivers that scrape the machine and its data plane directly, so no
Ava code participates and a box with no agents running still reports.

## What is scraped

- `host_metrics`, on every **collector-bearing** machine, every 30 s — cpu (utilization ratio
  only; the per-core cumulative `system.cpu.time` is disabled), load, memory,
  disk, filesystem, network.
- `prometheus/otelcol`, on every **collector-bearing** machine, every 30 s from the collector's
  loopback `127.0.0.1:8888` endpoint — exporter queue size/capacity, enqueue
  failure counters and process uptime. These are the delivery path's own
  observability: queue pressure and new drops must not hide behind a receiver
  that merely remains alive.
- `postgresql` + `redis`, every 60 s, on a **gateway-capable unit only** —
  this cluster's own data plane. Postgres is dialed DIRECT (never PgBouncer:
  `pg_stat_*` over a transaction-pooled session is not trustworthy) as the
  cluster's NOSUPERUSER owner role; Redis authenticates with the cluster
  secret, which is also its `requirepass`. A pure agent-runner's URLs point at
  the GATEWAY's data plane, so rendering those receivers there would only
  duplicate the gateway's series — `_data_plane_receivers` in
  `cli/commands/_otel_collector.py` omits them, and the rendered config is
  0600 because it carries the secret. The Postgres contrib receiver is also
  omitted when the direct URL has an empty password because that receiver
  rejects empty credentials; Redis remains enabled and unauthenticated.

Not a node_exporter / postgres_exporter / redis_exporter trio: the pinned
contrib collector already carries equivalent receivers, and one supervised
binary per machine is the whole point of the sidecar architecture.

## Labels

The infra receivers ride `metrics/infra`, a pipeline of their own, so the
host-identity processors never touch app metrics (which already carry
`machine` / `agent_id` datapoint attributes from the emitter):

- `resource_detection/host` supplies `host.name`;
- `resource/infra` pins `service.name=ava-infra`, which Prometheus's OTLP
  receiver turns into `job="ava-infra"` — without it the series would land
  under a synthesized unknown-service job;
- `transform/host_label` copies `host.name` onto every datapoint as `host`.
  A *resource* attribute would land in Prometheus's `target_info` instead of
  on the series, where no alert expression can select on it.

`host` is the OS hostname and deliberately not named `machine`: the Ava
machine name can differ, and one label with two meanings is worse than two
labels.

Cardinality is filtered at the source, not in Prometheus: synthetic mounts
(`/System/Volumes/*`, devfs, autofs …) and macOS's ~30 virtual interfaces
(awdl/llw/utun/…) are excluded, which took one machine's infra series from
~350 to ~180.

## One history

Prometheus is the only store that retains these. `ava status` and the status
page carry a single LIVE reading per machine (`shared/resource_sample.py`) so
they still answer on a deployment whose LGTM backend is down or was never
deployed; the retired `shared/resource_monitor.py` kept a parallel 300-sample
ring buffer, which meant two drifting answers to "what was the CPU on machine
X" and lost its history on every process restart.

## Not gated by the OTLP kill switch

`AVA_TELEMETRY_OTLP_ENABLED` is producer-scoped (the event dual-write, trace
recording, ship) and does not gate a sidecar that its role/home allows. These
scrapes are the collector's own, so a marked gateway or pure runner reports
host health even with the event stream reduced to its JSONL mirror only. An
unmarked gateway has no collector. Silencing an allowed collector means
stopping the sidecar
(`ava start --disable-service otel-collector`) or the backend (`ava lgtm off`);
with no backend reachable the Prometheus exporter's bounded retry drops them,
the same shed path app metrics already take. On a pure runner the exporter
targets the gateway collector's authenticated private receiver; only the
gateway collector writes to loopback Prometheus.

## Gaps

- **PgBouncer** has no OTel contrib receiver (`SHOW STATS` is not the Postgres
  wire protocol), so pool saturation is watched at Postgres — backends against
  `max_connections`.
- **Per-process attribution** is absent: the `host_metrics` process scraper is
  unsupported on macOS (what prod runs) and filters by process NAME, which
  cannot separate an Ava agent from any other `python3.12` on the box.

## Notes

- Alert rules R8-R12 read host/data-plane series. R14-R16 read collector
  delivery series: current queue ratio over 0.80, `increase` of the translated
  `otelcol_exporter_enqueue_failed_*_total` counters over 5 minutes, and a host
  seen in 24h whose `otelcol_process_uptime_total` disappeared for 5 minutes.
  The suffixes are Prometheus 3's OTLP translation, not the raw `:8888` names.
  Rules and the `ava-ops-main` dashboard (its "Host & data plane" section) live in
  `deploy/lgtm/config/grafana/provisioning/`.
- Metric names were read off a live Prometheus 3.13.2, not inferred from the
  OTLP names: `system_cpu_utilization_ratio`,
  `system_filesystem_utilization_ratio`, `postgresql_backends`,
  `postgresql_connection_max`, `redis_memory_used_bytes`, …
- The operator's reading loop is `conventions/runbook.md` ("The operator's SRE
  loop"); the thresholds are deployment config, never framework constants.
- Parent node: [[telemetry-otlp.ava.okf.md|OTLP export backend]].
