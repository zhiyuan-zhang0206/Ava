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
- **Idle detection**: SELECT agents from `agents_meta` where (`status = 'hibernating'` OR (`status = 'idling'` AND `lease_expires_at > now()`)) and **`last_active_at`** exceeds the threshold. An *idling* nudge target must hold an unexpired liveness lease — an idling row whose lease expired is a zombie the reaper is collecting, and nudging it would keep a corpse busy. *Hibernating* stays nudgeable with no lease at all: it has no process by design (swapped out), and the nudge is exactly how it wakes (see [[../../agent/agent.ava.okf.md|agent domain]] — the lease itself is [[agent/lease.ava.okf.md|Agent Liveness Lease]]). The idle clock is `last_active_at` (the real activity moment when the agent last completed an LLM turn), **not** `status_changed_at` — the latter is bumped on every status flip, including ops restart cycles like rollout quiesce / respawn / `ava update` (idling→restarting→…→idling), which would align/reset the entire fleet's idle clocks. `last_active_at` is only written by real turns (`agent/graph/_llm.py`); ops cycles do not run any turns for idle agents, so they don't touch it — ops events never reset the idle timer. **Swapping out (hibernating) also does not touch `last_active_at`**, so a hibernating agent's due-time algorithm is identical to when it was idling.
- **Include hibernating (wake-up)**: hibernating is an ops-layer memory swap-out state (process already killed). Heartbeat is a wake-up signal, so it must also wake them. The heartbeat inbound inserted for a hibernating agent has no process listening; it is picked up by the home machine's `HibernateController` swap-in polling (see pending inbound → spawn new process to take check-in), handled exactly the same as an idle agent that was never swapped out.
- **Respect heartbeat pause**: if `heartbeat_paused_until` is later than `last_active_at`, the next check-in is precisely set to `heartbeat_paused_until`
- **Deduplication**: skip agents that already have a `pending` inbound (NOT EXISTS guard), avoiding accumulation
- **Wake notification**: after INSERT of `heartbeat` inbound (kind='heartbeat'), sends a Redis best-effort publish (`shared.db._publish_inbound_wake`) — the target agent's claim loop subscribes to `<prefix>:inbound:<agent_id>` and receives it immediately; even if the publish is lost it doesn't block, the agent's 30s SELECT recheck guarantees delivery. PG `LISTEN/NOTIFY` has been retired (the `inbound_message_insert_notify` trigger has been dropped, no process listens anymore).
- **Cluster-wide**: not dependent on a local session, cross-machine wake-up (wherever the target agent runs, it is woken in its claim loop on that machine)

## Key Dependencies
- [[db.ava.okf.md]] — reads `agents_meta` table + writes `inbound_messages` table
- [[loop.ava.okf.md]] — after receiving a heartbeat, the agent appends a system note and decides to continue working / wait / terminate

## Entry Points
- `services/heartbeat/daemon.py` — `.venv/bin/python -m services.heartbeat.daemon`
- Watchdog keeps alive via `services/healthchecks/heartbeat.py`

## Notes
- heartbeat message tag = `NoteTag.HEARTBEAT`, agents can distinguish heartbeats from other wake-ups by this
- Unlike restarter: heartbeat is a gentle reminder (agent decides), restarter is a forced restart
