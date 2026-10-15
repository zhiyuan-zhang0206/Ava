---
type: doc
title: Impersonation relay supervision
description: Existing supervision opportunities, confirmed relay exit and fenced transport replacement.
tags: [agent-lifecycle, concurrency]
---

# Impersonation relay supervision

`supervise_relay` is the native supervision seam, called from two places: the
claim gate (native loop paused or resuming) and the held-controls pass
(services/agent_runner/agent_host/host.py `_apply_held_controls`) while an active lease
parks the agent outside the graph — the dispatcher's pending scan wakes rows
with an open lease periodically, pull-based from the database, so supervision
does not depend on wake delivery. Executor authority is separate from relay
transport: only explicit end, native runtime replacement or original TTL
ends the lease.
Confirmed host-supervised relay exit (the host's own child handle for Codex or Antigravity) bypasses heartbeat
freshness and startup grace on the next existing supervision pass; missing or
unknown evidence is not confirmed exit. Alive senders keep the existing
heartbeat/startup grace behavior. No additional scan or wake loop is introduced.
A stale or exited relay of any provider is recorded as degraded and is never
replaced: the lease keeps its one credential and runs to its original TTL or an
explicit end, and the controller re-requests
if it wants a new relay. Message attempts and ACK remain durable; exhaustion
pauses a message, never identity. The ended-lease notice uses the shared
`base.agents.impersonation.host_transport` owner; native handoff does not wait for
the host's single submission.

`agent/impersonation_process.py` owns process launch: its private pipe writes the
scoped credential to the child's stdin.
The native gate supplies its machine reader to relay authentication at the
existing boundaries, after partial-context and resource-closure guards.
