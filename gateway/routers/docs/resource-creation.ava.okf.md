---
type: doc
title: Schedule creation acceptance identities
description: Transactional recoverable creation without archived request/config copies.
tags: []
---

# Schedule creation acceptance identities

`POST /api/schedules` accepts an optional
`Idempotency-Key`. Shared `gateway.http.auth.request_principal.optional_request_key`
validates the 1–128 character key and its required `Idempotency-Scope:
principal-v1` using the existing verified credential binding. A key without that
scope, an unsupported scope, or a scope without a key is refused with 400.
CLI/browser mint one key per invocation and send it with the scope. Names remain
unique business constraints, separate from creation identity.

Shared `gateway/routers/receipts/creation.py` owns the validated request fingerprint
and original resource identity. The caller owns its transaction.

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
time. It stores no raw request, response, plugin config, script or token.
The retry supplies the same immutable body, verified against its fingerprint,
so the original creation view can be reconstructed without retaining a second
copy of potentially sensitive opaque plugin configuration. Receipts have no
expiry that could turn an old retry into a fresh duplicate creation.

This owner covers schedule resource creation only. Agent/fork creation
and launch-attempt recovery have separate business ownership. One-time MCP
credential creation is deliberately excluded; its token cannot be stored or
replayed through this table. No client outbox is involved.
