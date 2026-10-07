---
type: doc
title: MCP credential creation receipts
description: Human-only guarded creation recovers original metadata while preserving one-time plaintext-token disclosure.
tags:
- gateway
- mcp
- idempotency
---

# MCP credential creation receipts

`POST /api/keyed/v1/mcp/clients` requires the current human credential, a
1–128 character `Idempotency-Key`, and `Idempotency-Scope: principal-v1`.
Authentication precedes receipt lookup. Machine sessions and MCP client tokens
cannot create credentials or recover these receipts. The old
`POST /api/mcp/clients` remains one-shot; older routing has no safe fallback.

The guarded request freezes `name` and the canonical `McpClientScope` (`read`
or `write`). `creation_receipts.py` locks the authenticated principal/path/key,
checks the original request before mutable client state, and calls the native
credential transaction writer. Credential insertion and metadata receipt
commit together. Invalid scope fails before generating a token; a changed
request under the same key conflicts. Another key with an occupied name also
conflicts with the existing unique-name constraint.

The first committed response contains original `id`, `name`, `scope`,
`created_at`, `replayed=false`, and the plaintext `token`. A replay returns the
same creation metadata with `replayed=true` and `token=null`. This deliberate
secret exception means request acceptance is recoverable, but a lost original
response cannot recover its token. Receipts contain neither plaintext tokens
nor credential hashes, have no cleanup foreign key or expiry, and remain
historical after revocation, deletion, or name reuse. They do not describe the
credential's current usable state; use the existing metadata list for that.

After an ambiguous response, retry the same guarded operation to identify its
original client. If its token was lost, explicitly revoke that observed client
ID, then create a credential with a new intent and an unused name. Revocation
does not free names. No replay, transport retry, or metadata read implicitly
reissues or rotates a token. Reusing an old creation key after deletion still
returns the original ID, never a newly created credential.

`clients.py` owns credential creation and scope vocabulary. Its
`create_client_in_transaction` requires an already active transaction and
leaves commit to the caller; the legacy wrapper preserves its original
signature and one-time response. No client Outbox, UI/SDK activation, new
worker, or runtime rollout is included.
