# Reachability & credentials contract

Cross-machine dialing in a split cluster has exactly two facts to get right:
**where** each unit can be reached, and **what credential** authenticates the
call. This document is the single written contract for both. Code that
advertises an endpoint, dials a remote endpoint, or verifies a credential
references this file (see `base/cluster/machines.py`, `cli/commands/observability/otel_collector.py`,
`gateway/routers/pages.py`, `services/heartbeat/station_probe.py`).

## Endpoint advertisement

Every unit advertises **one inbound base URL** in its `machine_units.url` row
(`base.cluster.machines.register_self`), composed into the machine's
`machines.gateway_url`. `base.cluster.machines.unit_dial_url` is the single
definition of that URL — both writers (`ava start` and the ops daemon's boot
registration) share it, so two processes can never advertise different
addresses for the same unit.

| Capability set | Advertised url | Dialers |
|---|---|---|
| gateway (with or without station) | `http://<reachable_host()>:<gateway port>` | informational; the page proxy's SSRF allowlist |
| agent-runner (split or co-located) | `http://<reachable_host()>:<ops port>` | gateway cluster RPC (`ops/cluster_rpc.py`), spawn/lifecycle, roster + heartbeat probes |
| observability-station (pure) | `http://<reachable_host()>:<OTLP ingress port>` | remote gateway collector relay, the station health probe |

Three rules:

1. **The advertised host is always `reachable_host()`** (`AVA_MACHINE_HOST` >
   `localhost`). A unit never advertises its bare
   gateway URL and never hardcodes loopback: a machine with a reachable
   identity must advertise it, or every consumer that dials the advertised
   address dials the wrong host — the 2026-08-30 page-serve 400, where a
   gateway unit advertising `localhost` made the page proxy refuse the
   machine's real page servers (they bind `reachable_host()`, which is not in
   the loopback-only allowlist).
2. **Loopback advertisement is legal only when nothing remote dials it.** A
   zero-config single box advertises `localhost` and everything is co-located.
   A pure runner or pure station whose gateway is provably remote is refused
   at registration (`LoopbackDialUrlRefused`, `base.cluster.machines._reject_loopback_dial_url`)
   — the gateway would dial itself and report the peer online under the wrong
   identity (the 2026-07-18 runner incident).
3. **The station's advertised url is its OTLP ingress** (single source:
   the station unit's `AVA_TELEMETRY_OTLP_PORT`, default 4318) — the one station endpoint that
   authenticates with the telemetry token. The native backends (Loki 3100 /
   Prometheus 9090 / Grafana 3003) have no advertised url; they stay
   loopback-bound unless an operator widens the listen host,
   and they are never dialed cross-machine.
   Collector rendering and the station probe share `base.telemetry.station_endpoint`:
   they select the live pure-station advertisement on the configured
   `AVA_OBSERVABILITY_URL` host and preserve its port. Gateway/station and
   runner/station hybrids advertise gateway/ops URLs, so they are excluded by
   capability. Without a matching pure station, both consumers append
   `AVA_OBSERVABILITY_OTLP_PORT` (default 4318), which the operator must set to
   the remote station's actual ingress port. The consumer's local port never
   selects a remote target. Ambiguous advertisements fail rendering; discovery
   failure aborts rendering and makes the watchdog skip its probe round.


## SSRF guard

The page reverse proxy (`gateway/routers/pages.py`) dials only loopback or
the registering agent's home machine — its `agents_meta.machine` name plus
the hostnames its `machine_units` rows advertise. The unit advertisement is
therefore also the proxy's allowlist: a page server on a machine registers
its `reachable_host()` and is allowed because the unit advertises exactly
that host. Keeping the advertisement truthful (rule 1) is what keeps the
guard correct.

## Credentials

| Surface | Credential | Verifier |
|---|---|---|
| Gateway HTTP API, bootstrap, webhooks | `AVA_CLUSTER_SECRET` bearer (human/operator, gateway only) or the active write generation's machine API token (`AVA_API_TOKEN`) | `gateway.auth.request_principal.cluster_credential` (constant-time; a revoked generation's token never matches) |
| A unit's `/ops` | its write generation's gateway or runner API token | `base/cluster/auth.py` `verify_bearer_digest` over digests only |
| Gateway and station OTLP ingress (remote receiver) | the telemetry token (`HMAC(AVA_CLUSTER_SECRET)`; remote units hold only the token, from their capability) | otel-collector `bearertokenauth/cluster` extension |
| Data plane (Postgres/Redis) | split admin/runtime credentials, gateway-only admin | `conventions/data-plane-secret-split.md` |
| Loki/Prometheus backend APIs | none — loopback-only (`AVA_LGTM_LISTEN_HOST`) | n/a |
| Grafana UI | gateway session auth through `/grafana/*` proxy | gateway middleware; Grafana runs anonymous read-only |

An **empty `AVA_CLUSTER_SECRET`** is the zero-config single-box posture:
every surface serves unauthenticated on loopback, no machine token is
delivered, and any unit that would have to expose a remote ingress **fails
closed** (converge raises) rather than exposing an unauthenticated receiver. A
telemetry token (a non-empty secret on the gateway, an API-bearing capability
on a remote unit) on a unit with a non-loopback reachable host is what turns
remote ingress on.

The telemetry token is derived from the human secret, so there is
deliberately no second station secret to distribute, yet a unit holding it
learns nothing about the secret. It is scoped to the telemetry surface (the
collector's `bearertokenauth` extension) and carries no API semantics; it
is not part of the write generation (telemetry is not a write path) and
changes only when the human secret rotates.

## Verification

- Gateway / ops dialers present `Authorization: Bearer <token>` (their
  delivered machine API token, else an operator's human secret) and the
  receiver verifies in constant time; a blank configured secret never
  verifies (fails closed).
- The collector's remote receivers authenticate via the
  `bearertokenauth/cluster` extension; the remote relay exporters
  (`otlphttp/tempo|loki|prometheus` pointing at a remote station ingress)
  attach the same header.
- **Probe contract** (remote station health, `services/heartbeat/station_probe.py`):
  `POST <advertised station url>/v1/traces` with an empty
  `ExportTraceServiceRequest` and the telemetry token; any 2xx = alive. The
  probe dials the **advertised** address (rule 1), never a bare connect.
  Probe failure is fail-open: it alerts and never blocks local business.
