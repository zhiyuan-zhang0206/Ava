# Reachability & credentials contract

Cross-machine dialing in a split cluster has exactly two facts to get right:
**where** each unit can be reached, and **what credential** authenticates the
call. This document is the single written contract for both. Code that
advertises an endpoint, dials a remote endpoint, or verifies a credential
references this file (see `shared/machines.py`, `cli/commands/_otel_collector.py`,
`gateway/routers/pages.py`, `services/heartbeat/station_probe.py`).

## Endpoint advertisement

Every unit advertises **one inbound base URL** in its `machine_units.url` row
(`shared.machines.register_self`), composed into the machine's
`machines.gateway_url`. `shared.machines.unit_dial_url` is the single
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
   `$AVA_HOME/machine_host` > `localhost`). A unit never advertises its bare
   gateway URL and never hardcodes loopback: a machine with a reachable
   identity must advertise it, or every consumer that dials the advertised
   address dials the wrong host — the 2026-08-30 page-serve 400, where a
   gateway unit advertising `localhost` made the page proxy refuse the
   machine's real page servers (they bind `reachable_host()`, which is not in
   the loopback-only allowlist).
2. **Loopback advertisement is legal only when nothing remote dials it.** A
   zero-config single box advertises `localhost` and everything is co-located.
   A pure runner or pure station whose gateway is provably remote is refused
   at registration (`LoopbackDialUrlRefused`, `shared.machines._reject_loopback_dial_url`)
   — the gateway would dial itself and report the peer online under the wrong
   identity (the 2026-07-18 runner incident).
3. **The station's advertised url is its OTLP ingress** (single source:
   `AVA_TELEMETRY_OTLP_PORT`, default 4318) — the one station endpoint that
   authenticates with the cluster bearer. The native backends (Loki 3100 /
   Prometheus 9090 / Grafana 3003) have no advertised url; they stay
   loopback-bound unless an operator widens the listen host,
   and they are never dialed cross-machine.

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
| Gateway HTTP API, `/ops`, bootstrap, machine registration | `AVA_CLUSTER_SECRET` bearer | `shared/cluster_auth.py` `verify_bearer` (constant-time) |
| Station OTLP ingress (remote receiver) | `AVA_CLUSTER_SECRET` bearer | otel-collector `bearertokenauth/cluster` extension |
| Data plane (Postgres/Redis) | split admin/runtime credentials, gateway-only admin | `conventions/data-plane-secret-split.md` |
| Loki/Prometheus backend APIs | none — loopback-only (`AVA_LGTM_LISTEN_HOST`) | n/a |
| Grafana UI | gateway session auth through `/grafana/*` proxy | gateway middleware; Grafana runs anonymous read-only |

An **empty `AVA_CLUSTER_SECRET`** is the zero-config single-box posture:
every surface serves unauthenticated on loopback, and any unit that would
have to expose a remote ingress **fails closed** (converge raises) rather
than exposing an unauthenticated receiver. A non-empty secret on a unit with
a non-loopback reachable host is what turns remote ingress on.

The station's bearer is the **same** `AVA_CLUSTER_SECRET` as the control
plane — there is deliberately no second station secret. What differs is the
verification surface (the collector's `bearertokenauth` extension, not the
gateway admin middleware), so a credential valid for telemetry ingress is
still scoped to that surface and carries no admin semantics.

## Verification

- Gateway / ops dialers present `Authorization: Bearer <secret>` and the
  receiver verifies in constant time; a blank configured secret never
  verifies (fails closed).
- The collector's remote receivers authenticate via the
  `bearertokenauth/cluster` extension; the remote relay exporters
  (`otlphttp/tempo|loki|prometheus` pointing at a remote station ingress)
  attach the same header.
- **Probe contract** (remote station health, `services/heartbeat/station_probe.py`):
  `POST <advertised station url>/v1/traces` with an empty
  `ExportTraceServiceRequest` and the cluster bearer; any 2xx = alive. The
  probe dials the **advertised** address (rule 1), never a bare connect.
  Probe failure is fail-open: it alerts and never blocks local business.
