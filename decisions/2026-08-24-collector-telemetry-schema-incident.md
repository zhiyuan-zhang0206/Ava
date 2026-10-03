# Collector telemetry schema incident

PR #466 configured the per-unit collector self-metrics port with the
`service.telemetry.metrics.address` key, but otelcol-contrib 0.155.0 rejected
startup with `'service.telemetry.metrics' has invalid keys: address`, taking
every fleet collector down during rollout. The fix uses the supported
`readers[].pull.exporter.prometheus` form and keeps the self-scrape target on the
same loopback port; the pinned binary accepted this shape with
`otelcol-contrib validate`. Config-shape regression tests must mirror the schema
accepted by the pinned binary, with local validation against that binary as the
proof.
