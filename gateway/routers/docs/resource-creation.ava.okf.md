---
type: doc
title: Preset and schedule creation acceptance identities
description: Transactional recoverable creation without archived request/config copies.
tags: []
---

# Preset and schedule creation acceptance identities

`POST /api/presets` and `POST /api/schedules` accept an optional
`Idempotency-Key`. Shared `gateway.auth.request_principal.optional_request_key`
validates the 1–128 character key and explicit scope using the existing verified
credential binding. Legacy requests omit scope; unsupported scope or scope without
key is refused. CLI/browser mint one legacy key per invocation; they do not
silently enable principal-v1 or retry unsupported gateways. Names remain unique
business constraints, separate from creation identity.

The resource INSERT, initial schedule version, and receipt complete in one
transaction. Concurrent identical keys serialize before insertion. Changed
validated requests conflict with 409; a same-key retry returns the original 201
creation representation, including resource ID and timestamps. Retries do not
read a mutable resource as their receipt: rename, later edits, deletion and name
reuse cannot convert an old creation into a new resource. Replaying an acceptance
after deletion does not promise that resource still exists; current GET returns
404. New deliberate intent uses a new key and normal name constraints.

`resource_creation_receipts` stores only SHA-256 of the canonical validated
request, resource ID and original timestamps, plus the operation key/acceptance
time. It stores no raw request, response, preset plugin config, script or token.
The retry supplies the same immutable body, verified against its fingerprint,
so the original creation view can be reconstructed without retaining a second
copy of potentially sensitive opaque plugin configuration. Receipts have no
expiry that could turn an old retry into a fresh duplicate creation.

This owner covers preset and schedule resource creation only. Agent/fork creation
and launch-attempt recovery have separate business ownership. One-time MCP
credential creation is deliberately excluded; its token cannot be stored or
replayed through this table. No client outbox is involved.
