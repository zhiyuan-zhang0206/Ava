---
type: doc
title: "Telemetry read staleness guard"
description: "`gateway/telemetry_staleness.py` — gateway-side heartbeat checks that keep successful Loki/Prometheus reads from silently serving frozen telemetry."
tags:
- gateway
- telemetry
- observability
---

# Telemetry read staleness guard

## What it is

`gateway/telemetry_staleness.py` checks the newest `gateway_latency` heartbeat
in Prometheus and Loki after a composite telemetry read succeeds. Missing or
older-than-five-minute samples emit `telemetry_read_stale`; recovery emits
`telemetry_read_recovered`. Long outages re-emit the stale event every five
minutes. Heartbeat queries run at most once per minute; callers within that
window reuse the last verdict. Check errors fail open because each caller
retains its own backend exception degradation.

## Heartbeat and threshold

The gateway latency flusher emits once per active route every 60 seconds. That
signal advances independently of agent LLM activity and specifically identifies
the gateway exporter that went blind while `ava_llm_*` metrics continued on
2026-08-23. The Prometheus mirror is
`ava_gateway_latency_count_total`; Loki holds the original telemetry event.

The 300-second threshold is 5x the heartbeat cadence. It is not derived from
the 15-second metric export interval: 3x that interval would be 45 seconds and
would expire before a healthy 60-second heartbeat.

## Consumers and alerts

- `GET /api/fleet/graph` returns fetched data with `stale=true` and skips both
  Redis cache writes when the guard is stale.
- `ava-ops-gateway-metrics-silent` independently watches the Prometheus metric
  with `absent_over_time(...[5m])`; it does not depend on gateway events reaching
  Loki.
- `ava-ops-events-freshness` (R6) remains the whole-event-stream silence rule.
  Together the rules distinguish general stream silence from a gateway-metric
  exporter blind spot.

Parent node: [[gateway.ava.okf.md|Gateway]].
