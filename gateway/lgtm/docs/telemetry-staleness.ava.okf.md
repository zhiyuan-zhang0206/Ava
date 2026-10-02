---
type: doc
title: "Telemetry read staleness guard"
description: "`gateway/lgtm/telemetry_staleness.py` — gateway-side heartbeat check over `telemetry_events` that keeps a successful read from silently serving frozen telemetry."
tags:
- gateway
- telemetry
- observability
---

# Telemetry read staleness guard

## What it is

`gateway/lgtm/telemetry_staleness.py` checks the newest `gateway_latency` heartbeat
row in `telemetry_events` after a composite telemetry read succeeds (only the last ten
minutes are scanned). A missing or older-than-five-minute heartbeat emits `telemetry_read_stale`; recovery emits
`telemetry_read_recovered`. Long outages re-emit the stale event every five
minutes. Heartbeat queries run at most once per minute; callers within that
window reuse the last verdict. Check errors fail open because each caller
retains its own backend exception degradation.

## Heartbeat and threshold

The gateway latency flusher emits once per active route every 60 seconds. That
signal advances independently of agent LLM activity and specifically identifies
the gateway exporter that went blind while `ava_llm_*` metrics continued on
2026-08-23. The record of the heartbeat is its row in `telemetry_events`.

The 300-second threshold is 5x the heartbeat cadence. It is not derived from
the 15-second metric export interval: 3x that interval would be 45 seconds and
would expire before a healthy 60-second heartbeat.

## Consumers and alerts

- `GET /api/fleet/graph` returns the fetched graph with `telemetry_stale=true` (its
  `stale` flag stays for fallback data) when the guard is stale; the graph is still cached.
- `ava-ops-gateway-metrics-silent` independently watches the Prometheus metric
  with `absent_over_time(...[5m])`; it does not depend on gateway events reaching
  Loki.
- `ava-ops-events-freshness` (R6) remains the whole-event-stream silence rule.
  Together the rules distinguish general stream silence from a gateway-metric
  exporter blind spot.

Parent node: [[gateway.ava.okf.md|Gateway]].
