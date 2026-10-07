---
type: doc
title: Guarded credential creation write surface
description: Original MCP credential metadata recovery preserves one-time token disclosure.
tags:
- gateway
- idempotency
---

# Guarded credential creation

| Write | Contract | Evidence |
|---|---|---|
| `POST /api/keyed/v1/mcp/clients` | receipt, secret exception | original creation metadata survives response loss; plaintext token is never replayed |

The [credential owner](../../../mcp_server/docs/credential-creation.ava.okf.md)
defines human authentication, strict principal-v1 keys, one-transaction
acceptance, historical metadata and explicit revoke/new-intent recovery.
The legacy unguarded creation remains one-shot. Neither receipt recovery nor
transport retry implicitly rotates credentials or activates a client retry.
