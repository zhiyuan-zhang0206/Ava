---
type: doc
title: Database Layer
description: Durable inbound claims, checkpoint reconciliation and host-owned database access.
tags: []
---

# Database Layer

## What it is

`agent/db.py` implements kernel SQL for inbound claims and checkpoint
reconciliation. The agent host owns the workload and control pools; the
LangGraph saver shares the workload pool. Idle agents do not own connections
or a blocked graph invocation.

## Core Responsibilities

- **Durable queue**: `inbound_messages` holds work and native lifecycle commands.
  Claim dispatch handles chat, summaries, compaction, heartbeat, cancel,
  terminate, restart, resurrection and identity markers. Historical
  `restart_completed` messages remain readable; new restarts are observed by
  the successor admission.
- **Ownership**: claim locks metadata before inbound rows and validates the
  admitted generation, owner and lease. A stale or missing owner cannot claim
  another incarnation's work.
- **Checkpoint reconciliation**: startup, compaction and co-batch deferral use
  the same owner lock. They only mutate chat rows and never acknowledge a
  lifecycle command using missing checkpoint evidence.
- **Interrupt peek**: `has_pending_interrupt` reads pending external cancel or
  terminate commands so an in-flight LLM or exec can abort. Claim remains the
  authority for dispatch and acknowledgement.
- **Wake delivery**: queue writers publish a Redis wake after durable insertion.
  The host subscription handles delivery; its durable scan recovers missed
  publishes. Redis is a notification channel, not the queue authority.
- **Provenance compatibility**: kernel writers leave optional gateway credential
  evidence NULL when no gateway credential boundary was traversed.

## Entry Points

- `agent/db.py:claim_inbound_batch` — acquires the pool and row locks with bounded
  waits. Timeout rolls back; a later host wake or scan retries pending work.
- `agent/db.py:has_pending_interrupt` — read-only interrupt detection.
- `services/agent_host/daemon.py` — shared pool, checkpointer and wake lifecycle.
- `services/agent_host/host.py` — per-agent admission and checkpoint settlement.

## Key Dependencies

- [[graph.ava.okf.md]] — claim dispatch and graph execution
- [[agent/db/lifecycle-recovery.ava.okf.md]] — completion evidence
- [[lease.ava.okf.md]] — incarnation ownership

## Notes

An owned restart or terminate stays claimed until durable application and
observed completion, or explicit terminal failure/supersession. Accepted is
not completed. The fixed command target remains an audit fact after force
supersession; later commands and chat stay pending.

Older binaries with unconditional SQL still require a verified shutdown
barrier before upgrade. New ownership columns alone cannot fence old code.
