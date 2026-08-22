---
type: decision
title: Gateway is the observability access boundary
description: Browser observability reads authenticate once at the Ava gateway, which proxies loopback Grafana as one fixed Viewer identity; Grafana stores no cluster secret, every backend stays loopback-only, and authenticated OTLP on the gateway collector remains the sole remote write surface.
tags: [observability, security, grafana, gateway, websocket, otlp]
date: 2026-08-22
status: accepted
---

# Gateway is the observability access boundary

## Context

The LGTM host exposed Grafana on `*:3003` with anonymous Viewer access while
Loki, Tempo, and Prometheus were loopback-only. That made Grafana a second
cluster entry point with a different authentication model from the frontend
and gateway. "Viewer" prevented dashboard edits, but it still allowed queries
over every provisioned datasource — including raw session logs — from any
machine that could route to the host.

**User model.** A cluster operator already signs in to Ava with the cluster
secret and opens dashboards from the Ava frontend. They need read-only
dashboards, Explore, datasource queries, and Grafana Live; they do not need a
second Grafana account, password, session, or direct backend address.

Constraints:

- **True:** Grafana's HTTP auth-proxy protocol authenticates by a trusted
  header; browser WebSockets cannot attach a cluster-secret Bearer header.
- **True:** a pure runner must deliver traces/logs/metrics remotely, while the
  read backends and Grafana are co-located with the gateway.
- **Deliberate:** the machine/cluster is the trust boundary; local root and the
  Ava user are trusted, but Tailnet/LAN reachability alone is not
  authentication.
- **Deliberate:** observability must not add another copy or consumer of
  `AVA_CLUSTER_SECRET`.

## Decision

The Ava gateway is the only externally reachable observability user entry.

1. Grafana binds `127.0.0.1:3003`; Loki, Tempo, Prometheus, and the collector's
   local receiver remain loopback-only. The lifecycle derives Grafana
   `root_url` from `AVA_GATEWAY_URL` plus `/grafana/`; `:3003` is never a public
   URL.
2. Grafana anonymous and basic authentication, its browser login form/sign-out,
   and auth-proxy login tokens are disabled. Grafana's internal login service
   remains enabled because auth-proxy auto-signup uses its Grafana proxy client;
   neither basic nor form authentication registers a browser client. The gateway
   first verifies the existing
   Ava session cookie or cluster-secret Bearer, then removes Cookie,
   Authorization, and every caller-provided auth-proxy header and injects one
   fixed `ava-cluster-viewer` / `Viewer` identity. Auto-signup creates that
   identity as an organization Viewer. Grafana never receives or stores the
   cluster secret, and its `Set-Cookie` cannot cross back through the proxy.
3. The HTTP proxy exposes only the UI read surface: GET/HEAD and the
   `POST /api/ds/query` call dashboards need. Mutating API methods and POST
   targets are rejected at the gateway even if Grafana permissions drift.
   A 32-slot HTTP reservation is acquired before any request-body byte is read
   and held through response cleanup; combined with the 2 MiB per-request cap,
   retained completed bodies are capped at 64 MiB (128 MiB conservative peak
   while mutable buffers become immutable bytes). Ordinary response streams
   have a between-chunk deadline. Only a response Grafana identifies as
   `text/event-stream` may stay quiet, and those streams have a separate
   four-connection ceiling inside the same HTTP budget.
4. `/grafana/api/live/ws` has a dedicated bridge. Its handshake re-validates
   the Ava session/Bearer, requires Origin to equal the canonical
   `AVA_GATEWAY_URL` scheme/host/effective-port, and rejects credential query
   parameters before injecting the same fixed Viewer identity upstream.
   Text, binary, and close frames flow both ways under bounded message, queue,
   and connection limits. A gateway restart closes accepted sockets with 1012;
   cluster-secret rotation restarts the gateway, so every old session and Live
   connection is revoked together.
5. The LGTM marker is valid only on a gateway-capable unit. Gateway-only,
   hybrid `gateway,agent-runner`, and empty-secret single-box units use the same
   boundary; an empty secret means Ava itself is deliberately no-auth, but
   Grafana still has no anonymous identity without the gateway's fixed header.
   A pure runner carrying the marker fails before Docker starts.
6. Remote writes do not move. A pure runner's local collector sends all three
   signals to the gateway collector's exact private-address `:4318` receiver
   with `Authorization: Bearer AVA_CLUSTER_SECRET`. That authenticated OTLP
   receiver is the only cross-machine observability write surface; exporter
   IDs and file-backed queues remain unchanged.

Measurement boundary: gateway latency covers authentication through upstream
response headers. Lock-free OTLP observable gauges publish active/capacity for
HTTP, SSE, and WebSocket reservations, while one observable monotonic counter
publishes fast rejections; there is no per-transition event or LGTM query that
could create an observer feedback loop. A provisioned alert requires sustained
new rejections. Collector delivery metrics cover local acceptance through
remote export queue outcome. The periodic Grafana healthcheck certifies only
the loopback listener. `start.sh` additionally proves the auth proxy maps the
fixed non-admin identity to Viewer and that it can perform a read before
bring-up reports success; remote Ava-session auth is covered at the gateway.

## Alternatives considered

### 1. Keep anonymous Grafana on the Tailnet

Smallest change and Viewer blocks dashboard writes, but Tailnet reachability
becomes authorization. Viewers can issue arbitrary datasource queries, so this
keeps a second, wider read boundary and does not satisfy the cluster-secret
access model.

### 2. Give Grafana the cluster secret

Grafana could use basic auth or a shared credential. Rejected: it creates a
second credential store, a second login/session lifecycle, and a rotation
consumer. The gateway already knows how to authenticate browser and API
callers; duplicating that policy makes the boundary harder to reason about.

### 3. Gateway auth proxy with one fixed Viewer (chosen)

Authentication stays in one component, Grafana gets only an identity assertion
with least privilege, and the browser keeps one login. The cost is a real
reverse proxy: subpath redirects, streaming, WebSockets, header stripping, and
resource limits become gateway responsibilities. Those contracts are explicit
and regression-tested rather than delegated to deployment folklore.

### 4. Proxy Loki/Tempo/Prometheus directly and remove Grafana

This removes Grafana auth but pushes multiple backend protocols and query
languages into the frontend/gateway, discards the provisioned dashboards and
alerting UI, and expands the gateway surface. It is substantially more
complexity for a worse operator experience.

## Consequences

- A browser can no longer use `http://host:3003`; it uses the gateway's
  `/grafana/` path and the existing Ava login. Local healthchecks may still
  dial loopback ports because they are machine-internal, not user entry points.
- Grafana cannot operate independently of the gateway. This is intentional:
  the marker requires gateway co-location, and gateway/LGTM health surfaces
  report either half failing.
- The fixed principal makes per-human Grafana attribution unavailable. Ava is
  a single-operator cluster today; if multi-user authorization arrives, the
  gateway must inject distinct identities and the ADR must be revisited.
- Viewer is not a datasource row filter. Every authenticated Ava operator can
  query all provisioned observability data, matching the cluster trust model.
- Loopback is a machine boundary, not process authentication: any local process
  that can dial `127.0.0.1:3003` can assert an auth-proxy identity directly,
  including an administrator. Gateway callers cannot do this because their
  headers are stripped and replaced. This deployment therefore assumes a
  single trusted OS user; a multi-user host would require a Unix socket, host
  firewall/process isolation, or a different Grafana authentication boundary.
