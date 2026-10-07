---
type: doc
title: System Note Acceptance
description: Durable keyed system-note acceptance and mixed-version retry semantics.
tags: [gateway, agents]
---

## System-note acceptance

`POST /api/agents/{id}/system-note` accepts an optional `Idempotency-Key`.
The key commits with its `inbound_messages` row. Replay returns the original
id; changed recipient, content, source, task/tag or resurrection policy conflicts. The note keeps `kind=system_note`; its payload records
`delivery_resurrect` for immutable policy comparison. Keyless payloads keep their shape. SDK transport sends one operation key under the route contract. The
foundation rollout gate keeps automatic ambiguous-failure retries disabled
for newly keyed routes when gateway capability is unproven. A caller can
explicitly reuse its key against a compatible gateway.

Replay checks the durable receipt before current task ownership: later
reassignment does not invalidate an accepted message. Repeating wake is safe;
resurrection remains guarded by the exact pending inbound, so a handled note
cannot revive old work. Rows follow the existing inbound receipt retention
policy; there is no separate response-cache expiry or client outbox.
