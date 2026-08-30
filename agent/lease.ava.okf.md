---
type: doc
title: Agent Liveness Lease
description: The agent-process liveness lease — `agents_meta.lease_expires_at` written at claim, renewed by the run loop, cleared on exit — plus the single alive predicate every quiesce / reaper / frontend reader shares.
tags:
- agent-lifecycle
- liveness
- r1
---

# Agent Liveness Lease

## What it is

The **lease half of the registry × lease frame** ([[okf/design/r1-state-liveness/r1-state-liveness.ava.okf.md|R1 design]]) for agent processes: `agents_meta.lease_expires_at` is the liveness authority — a row with an unexpired lease IS a live process. The registry row ("should it exist?") and the lease ("is it alive?") answer separate questions; status carries lifecycle intent, the lease carries the process-level fact. A process that died without writing `terminated` leaves its status behind and the lease expires; a process that cannot renew (wedged, pre-lease code) is a zombie the reaper collects.

**The single alive predicate** lives in `shared/db.py`, one definition imported everywhere:

- `ALIVE_STATUSES = (running, idling)` — claim writes the lease while entering `running`; every other status is outside the predicate.
- `ALIVE_SQL = "status = ANY(%s) AND lease_expires_at > now()"` — the SQL half, interpolated by every reader so "alive" stays one definition across queries of every shape.
- `agent_is_alive(status, lease_expires_at)` — the Python half for row-based checks; a `None` lease (never granted — pre-lease code) reads as dead, matching the SQL fragment.

## Core Responsibilities

### Writers — claim / renew / clear

- **Claim**: `agent/_starting.py:claim_agent_row` writes `lease_expires_at = now() + AGENT_LEASE_TTL_S` (600 s) in the same UPDATE as `status='running'` — every spawn / resurrect / respawn path converges here, so a row is never alive without a lease.
- **Renewal**: `agent/loop.py:_renew_agent_lease_loop` — a background task re-arming the lease every `AGENT_LEASE_RENEW_INTERVAL_S` (60 s) for the whole graph lifetime, idle included. A failed renewal logs and retries; the TTL is 10x the interval, so a transient DB blip never reads as death. Scoped `status = ANY(ALIVE_STATUSES)`: a concurrent transition to restarting/terminated makes the renewal a no-op — the process is being replaced and must not renew a row that is no longer its own. The UPDATE is `agent/db.py:renew_agent_lease`.
- **Clear** (hygiene — the lease dies with the process): exit (`ops/ops_exit.py:_mark_exited_blocking`), resurrect / respawn (`ops/agent_wake.py` — the row returns to unclaimed `idling` with no lease; the next claim re-grants), and integrity-terminate all drop `lease_expires_at`.

### Observers

- **Reaper** — `ops/controllers/respawn.py:_collect_local_lease_zombies`: `running`/`idling` rows with an expired or NULL lease are zombies; a resident pid behind an expired lease is force-killed (wedged or pre-lease code), then the revive pass relaunches the row in place. The pass holds a post-outage grace window (`AGENT_LEASE_ZOMBIE_GRACE_S` = 180 s) after the restarter's own DB link recovers, so a paused-but-alive agent whose lease expired during the outage gets time to renew instead of being killed (2026-08-08 audit P1-2).
- **Wedged selector** requires an unexpired lease — lease-expired rows are the reaper's domain; lease-live-but-stuck rows stay wedged's.
- **Heartbeat daemon** (`services/heartbeat/daemon.py`): an *idling* nudge target must hold an unexpired lease (an expired idling row is a zombie — nudging it keeps a corpse busy); hosted mode drops the guard (a hosted idle row has no lease concept).
- **Quiesce readers** (all switched to the predicate): `signal_live_agents_restart` / `list_live_agent_ids` / `mark_agents_restarting` in `shared/db.py`; count readers `cli/commands/_cluster_health.py` (agent-population gate), `cli/commands/_cluster_rollback.py` (rollback notify), `shared/core_metrics_panels.py` (live-agents panel).

## Key Dependencies

- [[startup.ava.okf.md]] — the claim is Stage 2 of the startup sequence
- [[loop.ava.okf.md]] — the renewal task runs for the whole graph lifetime
- [[db.ava.okf.md]] — `renew_agent_lease` on the kernel DB layer
- [[okf/design/r1-state-liveness/r1-state-liveness.ava.okf.md|R1 state & liveness design]] — the registry × lease frame
- `shared/deploy_timing.py` — `AGENT_LEASE_TTL_S` / `AGENT_LEASE_RENEW_INTERVAL_S` / `AGENT_LEASE_ZOMBIE_GRACE_S` / `AGENT_LEASE_SUSPEND_GAP_S`

## Entry Points

- `agent/_starting.py:claim_agent_row` — lease birth (claim)
- `agent/loop.py:_renew_agent_lease_loop` — lease renewal
- `agent/db.py:renew_agent_lease` — the renewal UPDATE
- `ops/controllers/respawn.py:_collect_local_lease_zombies` — zombie collection
- `shared/db.py:agent_is_alive` / `ALIVE_SQL` — the predicate

## Notes

- Timing: TTL 600 s = 10x the 60 s renewal interval, with the reaper running every 30 s — a healthy agent can miss several renewals without being collected.
- The R1 migration granted leases to every then-live agent (status + pid, TTL 600 s); a dead-pid row's backfill lease was the migration's own cleanup — it expired and the reaper collected it.
- `agents_meta.last_compact_at` arrived in the same migration — a synchronous compact stamp (replacing the events-table OFFSET-1 read), a fact column, not a lease; it belongs to the compact domain.
