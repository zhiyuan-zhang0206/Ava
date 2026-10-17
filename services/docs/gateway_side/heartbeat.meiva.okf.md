---
type: doc
title: Heartbeat — Idle Agent Heartbeat
description: Gateway's idle agent check scheduler — every `MEIVA_HEARTBEAT_INTERVAL_SECONDS` (default 5 minutes) scans parked longer than
tags: []
---

# Heartbeat — Idle Agent Heartbeat

## What is it
Gateway's idle agent check scheduler — every `MEIVA_HEARTBEAT_INTERVAL_SECONDS` (default 5 minutes) scans idle agents that have been parked longer than `MEIVA_HEARTBEAT_IDLE_THRESHOLD_SECONDS` (default 5 minutes) and whose heartbeat is not paused, inserting a `heartbeat` inbound message for each agent to trigger wake-up. Cluster-wide (cross-machine), not limited to local.

**Role affiliation**: gateway side (pure agent-runner does not run) — `ServiceSpec.capabilities=GATEWAY` in `services/supervision/roster/spec.py`; `services_for_capabilities` receives explicit roles and the root's retained `MachineIdentity`, whose `role()` supplies the local capability set.

## Core Responsibilities
- **Idle detection**: Select eligible `idling` identities whose real `last_active_at` and last heartbeat are old enough. No per-agent PID or live-process lease is required. The insert atomically stamps `last_heartbeat_at`, preventing repeated reminders when a wake performs no model work. Maintenance and circuit-breaker policy remain separate gates; operational status changes do not reset the real activity clock.
- **Respect heartbeat pause**: the pause is a lower bound. Native idle eligibility
  takes the latest of pause expiry, real activity plus idle threshold and jitter,
  and the last heartbeat plus its cadence/backoff interval.
- **Nudge backoff (B7)**: an agent that repeatedly burns nudges — `MEIVA_HEARTBEAT_BACKOFF_CONSECUTIVE_NOOP_NUDGES` (default 3) consecutive check-ins with no real inbound and no pause — gets its reminder floor stretched to `heartbeat_interval * 2^level` (`heartbeat_backoff_level` on `agents_meta`, capped at 24h, `power(2.0, level)` in the selection SQL). The consecutive-no-op counter is in-process; the level persists across daemon restarts. A real inbound (`inbound_messages.kind <> 'heartbeat'` after `last_heartbeat_at`) or an agent pause resets the level to 0 (`_sweep_backoff_resets` covers agents the daemon is not tracking). Raises emit `heartbeat_backoff_raised`, resets emit `heartbeat_backoff_reset`. Complementary to `meiva.self.pause_heartbeat()` — the agent's own pause always wins; the platform backoff is the fallback for agents that never pause.
- **Deduplication**: skip agents with a recent `pending` inbound within
  `STALE_PENDING_S`; stale rows do not indefinitely suppress reminders.
- **Wake notification**: after INSERT of `heartbeat` inbound (kind='heartbeat'), sends a Redis best-effort publish (`base.db._publish_inbound_wake`) — the target agent's claim loop subscribes to `<prefix>:inbound:<agent_id>` and receives it immediately; even if the publish is lost it doesn't block, the agent's 30s SELECT recheck guarantees delivery.
- **Cluster-wide**: not dependent on a local session, cross-machine wake-up (wherever the target agent runs, it is woken in its claim loop on that machine)
- **Mounted-console liveness**: the one-minute machine/lease pass keeps lifecycle
  intent and reachability separate in `agents_meta.status` / `liveness_state`.
  `machine_probe.consecutive_failures` counts failed probes until recovery,
  and each failed probe of an unpaused rollout target emits a
  `machine_probe_failed` event (machine + `consecutive_failures`) — the signal
  the Grafana "machine offline" rules grade (grace window, WARNING -> ERROR).
  A paused machine is not a target, so it emits nothing.
  Edges entering or leaving `offline` publish the canonical `agent_updated`
  snapshot after commit, so the existing frontend fold updates immediately;
  `unknown → online` is not broadcast because both render online and a
  fleet-sized first-pass burst would carry no visible change.

## Key Dependencies
- [[agent/db/docs/db.meiva.okf.md]] — reads `agents_meta` table + writes `inbound_messages` table
- [[loop.meiva.okf.md]] — after receiving a heartbeat, the agent appends a system note and decides to continue working / wait / terminate

## Entry Points
- `services/wake/heartbeat/daemon.py` — `.venv/bin/python -m services.wake.heartbeat.daemon`
- The application root supervises the daemon through the roster's `/healthz` identity probe (`services/supervision/roster/healthz.py`).

## Notes
- Native idle selection requires no PID, orders eligible agents by activity age
  and caps each batch. Jitter uses a whole-second span so SQL and the inspector's
  Python modulo agree; zero disables it. Active impersonation leases have their
  own activity baseline and gates in the selection SQL.
- Idle-minute observations keep the PostgreSQL clock and cross the SQL boundary as double precision, matching the float slack used by next-cycle check-in reconciliation.
- heartbeat message tag = `NoteTag.HEARTBEAT`, agents can distinguish heartbeats from other wake-ups by this

The liveness pass retains machine reachability and agent lease observation.
It runs once at start, then every `LIVENESS_PASS_INTERVAL_S`, and it also records
one `machine_status_snapshot` row for every unpaused agent-runner (rollout targets
and staging/stopped hosts alike); judging and the failure signal stay with the rollout targets.
The gateway's roster read renders from those rows (`gateway/cluster/snapshots.py`)
and dials inline only a missing or stale one (`?fresh=true` dials everything).


## Liveness test boundary

The liveness and probe-failure tests in `services/wake/heartbeat/tests/` use
real database merges with injected probes. Seeded failure-count cases call the
merge directly so a successful probe cannot reset their setup. Event assertions
cover offline edges and the silent `unknown` to `online` transition. Paused-machine
exclusion is proved by probe calls, absent probe rows and absent events; the
fixture's endpoint is never dialed. The timeout case checks the configured
12-second argument, not elapsed timeout enforcement or live ops reachability.

## Process composition

The heartbeat executable and standalone station probe each capture one loaded
image and retain their own `CodeVersion` / `ProcessDbGate`. Work and telemetry
factories share the entry's gate. Heartbeat health reports the same captured
image; the station probe passes its factory into `resolve_target(database=...)`
and keeps the configured-target no-op and fail-open lookup/probe boundaries.
The heartbeat root also retains one lazy `MachineIdentity` and supplies it to
its work loops and telemetry; its database factory borrows the same owner's
`host` reader without resolving it before a direct-host operation needs it.

The service entry retains its prepared `ConfigBoot` and resolved unit runtime
path. Endpoint and pidfile operations borrow those inputs through
`ServiceEndpoints.from_boot`; each original lookup reads the same live view.
The work and logging database factory calls `db_config_from_boot` at each
handle creation and retains the entry's gate and lazy machine host reader.
Schema checks, health-before-pool ordering and shutdown stay at their original
boundaries. Business-specific configuration slices keep their existing readers.
