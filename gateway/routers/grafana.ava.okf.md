---
type: doc
title: Grafana browser access boundary
description: The gateway's authenticated, read-only HTTP and WebSocket proxy from cluster users to loopback-only Grafana.
tags:
- gateway
- observability
- security
---

# Grafana browser access boundary

`gateway/routers/grafana.py` mounts the only external Grafana entry at
`/grafana/*`, outside `/api`. Grafana, Loki, Tempo, and Prometheus remain bound
to loopback; Grafana never receives or stores the cluster secret. The gateway
authenticates each request by its normal session cookie or cluster Bearer
credential, strips Cookie, Authorization, and spoofable auth-proxy headers,
then injects one fixed Viewer identity before streaming the request to Grafana.

The HTTP surface admits GET and HEAD plus the read-only Grafana query endpoint
`POST /grafana/api/ds/query`. Other mutating POST routes are rejected. Request
bodies are bounded before they are read. The 32-slot HTTP budget retains at
most 64 MiB of completed request bodies; bytearray-to-bytes conversion can
briefly double that body storage to 128 MiB. Responses stream with a
between-chunk timeout and connection budget rather than a total-byte cap.
Upstream redirects cannot escape the gateway origin, upstream cookies cannot
cross the boundary, and proxy clients ignore ambient process proxy variables.

Grafana Live uses `/grafana/api/live/ws`. Its handshake repeats cluster auth
and requires the browser Origin to exactly match configured `AVA_GATEWAY_URL`
(scheme, host, and effective port). Capacity is reserved before the upstream
dial, WebSocket frames are bounded at the Uvicorn transport and relay layers,
and text, binary, and close frames retain their protocol meaning. A gateway
restart or cluster-secret rotation closes existing sockets; clients reconnect
through a fresh authenticated handshake.

The shared OTLP metrics backend observes HTTP, SSE, and WebSocket active slots,
capacity, and cumulative fast rejections from lock-free process snapshots. It
does not emit an event per transition or query LGTM, avoiding a feedback loop.

The compose stack configures Grafana auth-proxy mode for this fixed Viewer and
disables direct login. `deploy/lgtm/start.sh` does not declare readiness until
Grafana reports that identity as a non-admin Viewer and a real dashboard search
succeeds.

Parent: [[gateway/routers/routers.ava.okf.md|Gateway Routers]]. Write-side OTLP
topology: [[shared/telemetry-otlp/telemetry-otlp.ava.okf.md]].
