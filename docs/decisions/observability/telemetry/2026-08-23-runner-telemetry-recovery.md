# Runner telemetry recovery

The gateway exporter and trace recorder now treat a collector miss at process
boot as a recoverable episode. The event backend probes at most once per five
minutes, emits disabled/recovered status into the unified stream, and preserves
those reports in the JSONL mirror while OTLP is unavailable. Trace recording
uses the same interval through one serialized daemon retry loop; disk-watermark
degradation remains operator state and does not start that loop.

Read surfaces no longer equate a successful backend query with fresh data. The
gateway's own 60-second `gateway_latency` event is the heartbeat because it
advances independently of agent activity and identifies the exporter that was
blind during the 2026-08-23 incident. Prometheus and Loki samples older than
five minutes make the fleet graph visibly stale and prevent both cache writes.

The Prometheus-native `ava-ops-gateway-metrics-silent` rule watches the same
heartbeat without depending on gateway events reaching Loki. The existing R6
rule remains the broader whole-event-stream silence detector. The OTLP switch
is documented as JSONL-only when off; the retired Postgres events copy is a
read-only archive, not a fallback.
