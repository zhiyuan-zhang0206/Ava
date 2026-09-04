---
type: doc
title: "Gateway runtime metrics"
description: "Process resources, event-loop responsiveness, and SSE lifecycle metrics emitted by the gateway into the unified OTLP telemetry path."
tags:
- gateway
- telemetry
- observability
- metrics
---

# Gateway runtime metrics

`gateway/_runtime_metrics.py` owns the process-local measurements that cannot
be reconstructed from request logs: gateway CPU, RSS, open file descriptors,
event-loop responsiveness, and live SSE connection depth.

## Process and event loop

The monitor schedules a callback every second on uvicorn's running asyncio
loop. Callback delay is event-loop lag. Every 60 seconds it emits the maximum
observed lag and the number of ticks delayed by at least 100 ms. Scheduling the
next tick from the recovery time avoids a catch-up callback storm after a long
freeze. The same flush reads non-blocking `psutil.Process` CPU, RSS, and file
descriptor values.

The unified event-to-OTLP mapping produces:

- `ava_gateway_process_cpu_percent`, `ava_gateway_process_rss_bytes`, and
  `ava_gateway_process_fd_count` absolute gauges;
- `ava_gateway_event_loop_lag_milliseconds` as a histogram with explicit
  1 ms through 300 s buckets; and
- `ava_gateway_event_loop_slow_ticks_total` as the count of delayed monitor
  ticks.

A process-sampling failure is logged and the callback reschedules itself. The
event-loop sample is emitted before the process read, so a denied or vanished
`psutil` reading cannot hide loop stalls.

## SSE lifecycle

Both SSE generator modes bracket successful Redis subscriptions with
`sse_opened` / `sse_closed`. The emitted `sse` event maps to
`ava_sse_active_connections` (absolute gauge by mode) and monotonic
`ava_sse_opened_total` / `ava_sse_closed_total` counters. Startup publishes a
zero depth for both modes so an idle gateway exposes the gauge before its first
connection.

Parent node: [[gateway.ava.okf.md|Gateway]].
