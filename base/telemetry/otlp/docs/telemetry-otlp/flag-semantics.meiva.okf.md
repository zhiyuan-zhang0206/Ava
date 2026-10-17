---
type: doc
title: "OTLP flag semantics & endpoints"
description: "The read side of OTLP export — no gateway reader consumes the Loki copy; the enablement flags and their startup-apply semantics; and how gateway and runner roles derive their endpoint defaults."
tags:
- base
- telemetry
- otlp
- observability
---

# OTLP flag semantics & endpoints

No gateway reader consumes the Loki copy; every reader uses `telemetry_events`.

**Flag semantics** — `MEIVA_TELEMETRY_OTLP_ENABLED` /
`MEIVA_TELEMETRY_OTLP_ENDPOINT` (default `http://127.0.0.1:4318` — the standard
local OTLP HTTP port, derived from `MEIVA_TELEMETRY_OTLP_PORT`'s default) are
startup-applied
(`restart_required=all`); `base/config` has no live-reload — flip +
restart applies changes. Off leaves only the JSONL event sink: Loki/Prometheus
and their read surfaces stop advancing, with no Postgres fallback. Converge
renders a role-specific collector config only for the marked LGTM gateway or a
pure runner. An explicit `MEIVA_TELEMETRY_OTLP_ENDPOINT` bypasses the producer
gate but does not install a collector on an unmarked gateway.
The LGTM gateway uses `MEIVA_TELEMETRY_TEMPO_ENDPOINT` (default loopback host
port 14318), `MEIVA_TELEMETRY_LOKI_URL` and
`MEIVA_TELEMETRY_PROMETHEUS_URL` as gateway-local backend URLs. Pure runners do
not consume those loopback backend URLs: they derive the gateway collector
ingress host from `MEIVA_GATEWAY_URL` and authenticate with the telemetry
token. The remote receiver exists only on a gateway with an exact
non-loopback `MEIVA_MACHINE_HOST`; combined single-box hosts collapse to the local
receiver.

Parent node: [[telemetry-otlp.meiva.okf.md|OTLP export backend & trace ship to Tempo]].
