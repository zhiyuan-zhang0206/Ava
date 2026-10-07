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
transport: only confirmed provider-anchor death, explicit end or original TTL
ends the lease. Unknown liveness remains visible until the original deadline.
Confirmed Codex relay exit bypasses heartbeat freshness and startup grace on
the next existing supervision pass. Known child handles or the recorded birth
provide this observation; missing or unknown evidence is not confirmed exit.
Alive senders keep the existing heartbeat/startup grace behavior. No additional
scan or wake loop is introduced. Recovery still retires the old recorded process
birth, then claims one
transport generation under the lease lock. The new child receives its private
credential only after its birth is persisted. Message attempts and ACK remain
durable across replacement; exhaustion pauses a message, never identity.
Legacy generation-zero relays missing birth evidence remain visibly degraded
rather than guessed or adopted. Independent ended-lease notices use the existing
host scan and shared `base.agents.impersonation.host_transport` owner; native
handoff does not wait for host submission or its retry receipts.
