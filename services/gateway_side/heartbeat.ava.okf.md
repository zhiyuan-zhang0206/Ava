---
type: doc
title: Heartbeat — Idle Agent Heartbeat
description: Gateway's idle agent check scheduler — every `AVA_HEARTBEAT_INTERVAL_SECONDS` (default 5 minutes) scans parked longer than
tags: []
---

# Heartbeat — Idle Agent Heartbeat

## What is it
Gateway's idle agent check scheduler — every `AVA_HEARTBEAT_INTERVAL_SECONDS` (default 5 minutes) scans idle agents that have been parked longer than `AVA_HEARTBEAT_IDLE_THRESHOLD_SECONDS` (default 5 minutes) and whose heartbeat is not paused, inserting a `heartbeat` inbound message for each agent to trigger wake-up. Cluster-wide (cross-machine), not limited to local.

**Role affiliation**: gateway side (pure agent-runner does not run) — `ServiceSpec.capabilities=_GATEWAY` in `ops/spec.py`; roster derived by `services_for_capabilities` intersecting with local `machine_role()`.

## Core Responsibilities
- **Idle detection**: SELECT agents from `agents_meta` where (`status = 'idling'` AND `lease_expires_at > now()` — process mode; hosted mode drops the lease guard: a hosted idle row has no lease concept), **`last_active_at`** exceeds the threshold, and `last_heartbeat_at` is absent or at least the configured heartbeat interval old. The insert atomically stamps `last_heartbeat_at`, so a heartbeat that produces no LLM work cannot be reinserted every dispatch step or after a daemon restart. An *idling* nudge target must hold an unexpired liveness lease — an idling row whose lease expired is a zombie the reaper is collecting, and nudging it would keep a corpse busy (see [[../../agent/agent.ava.okf.md|agent domain]] — the lease itself is [[agent/lease.ava.okf.md|Agent Liveness Lease]]). The idle clock is `last_active_at` (the real activity moment when the agent last completed an LLM turn), **not** `status_changed_at` — the latter is bumped on every status flip, including ops restart cycles like rollout quiesce / respawn / `ava update` (idling→restarting→…→idling), which would align/reset the entire fleet's idle clocks. `last_active_at` is only written by real turns (`agent/graph/_llm.py`); ops cycles do not run any turns for idle agents, so they don't touch it — ops events never reset the idle timer.
- **Respect heartbeat pause**: if `heartbeat_paused_until` is later than `last_active_at`, the next check-in is precisely set to `heartbeat_paused_until`
- **Nudge backoff (B7)**: an agent that repeatedly burns nudges — `AVA_HEARTBEAT_BACKOFF_CONSECUTIVE_NOOP_NUDGES` (default 3) consecutive check-ins with no real inbound and no pause — gets its reminder floor stretched to `heartbeat_interval * 2^level` (`heartbeat_backoff_level` on `agents_meta`, capped at 24h, `power(2.0, level)` in the selection SQL). The consecutive-no-op counter is in-process; the level persists across daemon restarts. A real inbound (`inbound_messages.kind <> 'heartbeat'` after `last_heartbeat_at`) or an agent pause resets the level to 0 (`_sweep_backoff_resets` covers agents the daemon is not tracking). Raises emit `heartbeat_backoff_raised`, resets emit `heartbeat_backoff_reset`. Complementary to `ava.self.pause_heartbeat()` — the agent's own pause always wins; the platform backoff is the fallback for agents that never pause.
- **Deduplication**: skip agents that already have a `pending` inbound (NOT EXISTS guard), avoiding accumulation
- **Wake notification**: after INSERT of `heartbeat` inbound (kind='heartbeat'), sends a Redis best-effort publish (`shared.db._publish_inbound_wake`) — the target agent's claim loop subscribes to `<prefix>:inbound:<agent_id>` and receives it immediately; even if the publish is lost it doesn't block, the agent's 30s SELECT recheck guarantees delivery. PG `LISTEN/NOTIFY` has been retired (the `inbound_message_insert_notify` trigger has been dropped, no process listens anymore).
- **Cluster-wide**: not dependent on a local session, cross-machine wake-up (wherever the target agent runs, it is woken in its claim loop on that machine)
- **Mounted-console liveness**: the one-minute machine/lease pass keeps lifecycle
  intent and reachability separate in `agents_meta.status` / `liveness_state`.
  `machine_probe.transition_since` records the first failed probe until
  recovery. The same episode stays silent during the normal-recovery budget,
  fires WARNING after `AVA_ALERTS_TRANSITION_WARNING_SECONDS`, and escalates
  in place to ERROR after `AVA_ALERTS_TRANSITION_ERROR_SECONDS`. A live cluster
  deploy or that host's updater lease explains the transition without resetting
  its start; unreadable lease state explains nothing.
  Edges entering or leaving `offline` publish the canonical `agent_updated`
  snapshot after commit, so the existing frontend fold updates immediately;
  `unknown → online` is not broadcast because both render online and a
  fleet-sized first-pass burst would carry no visible change.

## Key Dependencies
- [[db.ava.okf.md]] — reads `agents_meta` table + writes `inbound_messages` table
- [[loop.ava.okf.md]] — after receiving a heartbeat, the agent appends a system note and decides to continue working / wait / terminate

## Entry Points
- `services/heartbeat/daemon.py` — `.venv/bin/python -m services.heartbeat.daemon`
- Watchdog keeps alive via `services/healthchecks/heartbeat.py`

## Notes
- heartbeat message tag = `NoteTag.HEARTBEAT`, agents can distinguish heartbeats from other wake-ups by this
- Unlike restarter: heartbeat is a gentle reminder (agent decides), restarter is a forced restart
