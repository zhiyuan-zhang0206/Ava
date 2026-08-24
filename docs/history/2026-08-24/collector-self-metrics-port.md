# Collector self-metrics port is per unit

## Context

An OTel Collector exposes its own Prometheus metrics on port 8888 by default.
When one machine carries more than one Ava unit, each unit starts a collector;
the second process cannot bind the same loopback port and fails to start. This
occurred when the WSL box carried both production and preview units.

## Decision

`AVA_OTELCOL_METRICS_PORT` is a host-scoped, per-unit setting with default
8888. Converge bakes `localhost:<port>` into both the collector's
`service.telemetry.metrics.address` and its `prometheus/otelcol` self-scrape
target. The watchdog queue-pressure probe reads the same setting rather than a
fixed port. Co-located units must use distinct values, while a single-unit
machine keeps the existing default.
