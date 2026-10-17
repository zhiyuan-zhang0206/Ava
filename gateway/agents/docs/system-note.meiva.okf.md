---
type: doc
title: System Note Acceptance
description: Durable keyed system-note acceptance and retry semantics.
tags: [gateway, agents]
---

## System-note acceptance

`POST /api/agents/{id}/system-note` requires `Idempotency-Key` and
`Idempotency-Scope: principal-v1`.
The key commits with its `inbound_messages` row. Replay returns the original
id; changed recipient, content, source, task/tag or resurrection policy conflicts. The note keeps `kind=system_note`; its payload records
`delivery_resurrect` for immutable policy comparison. Missing keys fail before mutation. Both SDK layers require an explicit key;
watcher retries retain the same key for one notification, and the SDK
transport retries an ambiguous failure under it.

Replay checks the durable receipt before current task ownership: later
reassignment does not invalidate an accepted message. Repeating wake is safe;
resurrection remains guarded by the exact pending inbound, so a handled note
cannot revive old work. Rows follow the existing inbound receipt retention
policy; there is no client outbox.
