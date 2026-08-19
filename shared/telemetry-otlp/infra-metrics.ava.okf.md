---
type: doc
title: "Infrastructure metrics — the sidecar's own scrapes"
description: "The traditional SRE layer (issue #46): the per-machine OTel Collector sidecar scrapes host_metrics everywhere plus postgresql + redis on a gateway-capable unit, on its own `metrics/infra` pipeline, landing in Prometheus under `job=\"ava-infra\"` with a `host` label — no node_exporter, no Ava producer code, and exactly one retained host history."
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

- `host_metrics`, on **every** machine, every 30 s — cpu (utilization ratio
  only; the per-core cumulative `system.cpu.time` is disabled), load, memory,
  disk, filesystem, network.
- `postgresql` + `redis`, every 60 s, on a **gateway-capable unit only** —
  this cluster's own data plane. Postgres is dialed DIRECT (never PgBouncer:
  `pg_stat_*` over a transaction-pooled session is not trustworthy) as the
  cluster's NOSUPERUSER owner role; Redis authenticates with the cluster
  secret, which is also its `requirepass`. A pure agent-runner's URLs point at
  the GATEWAY's data plane, so rendering those receivers there would only
  duplicate the gateway's series — `_data_plane_receivers` in
  `cli/commands/_otel_collector.py` omits them, and the rendered config is
  0600 because it carries the secret.

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
recording, ship) and has never gated the sidecar process itself. These scrapes
are the collector's own, so a machine reports host health even with the event
stream on Postgres only. Silencing them means stopping the sidecar
(`ava start --disable-service otel-collector`) or the backend (`ava lgtm off`);
with no backend reachable the Prometheus exporter's bounded retry drops them,
the same shed path app metrics already take.

## Gaps

- **PgBouncer** has no OTel contrib receiver (`SHOW STATS` is not the Postgres
  wire protocol), so pool saturation is watched at Postgres — backends against
  `max_connections`.
- **Per-process attribution** is absent: the `host_metrics` process scraper is
  unsupported on macOS (what prod runs) and filters by process NAME, which
  cannot separate an Ava agent from any other `python3.12` on the box.

## Notes

- Alert rules R8-R12 and the `ava-host-dataplane` Grafana dashboard read these
  series; both live in `deploy/lgtm/config/grafana/provisioning/`.
- Metric names were read off a live Prometheus 3.13.2, not inferred from the
  OTLP names: `system_cpu_utilization_ratio`,
  `system_filesystem_utilization_ratio`, `postgresql_backends`,
  `postgresql_connection_max`, `redis_memory_used_bytes`, …
- The operator's reading loop is `conventions/runbook.md` ("The operator's SRE
  loop"); the thresholds are deployment config, never framework constants.
- Parent node: [[telemetry-otlp.ava.okf.md|OTLP export backend]].
