---
type: doc
title: "Credential-Scoped Idempotency"
description: "Server-bound credential principals and the principal-scoped idempotency storage identity, independent of caller provenance."
tags:
- gateway
- identity
---

# Credential-scoped idempotency

The authentication middleware binds an `AuthPrincipal` only after successful
credential validation. A cluster bearer is one shared cluster principal, not a
different principal per tool label. Validated browser sessions authenticate that
same stable cluster administrator, but only while the credential that minted
them is current ([[web-sessions.meiva.okf.md]]); rotating a cookie does not
change principal.
The API always authenticates, so a verified principal exists on every request that passes the middleware. Caller/source JSON cannot bind one.

Alongside that authorization principal, the middleware stores the narrower
credential fact used for inbound audit rows: `cluster_bearer` (the human
secret), `machine_token:<class>` (the active write generation's machine API
token, `cluster_credential`), `user_session` (a browser session the human secret
minted) or `machine_session:runner` (one a runner token minted, a unit's managed
browser). Both bearers authenticate the
same cluster administrator; a pending generation's token never matches. Scoped webhook and MCP boundaries establish their own
`webhook:<provider>` or `mcp_client:<id>` fact after authenticating outside the
cluster middleware. The fact is never copied from request JSON, and it grants
authority in one place only: the MCP client admin routes mint tokens bound to
no write generation, so `require_human_credential` admits only the human facts
(`cluster_bearer`, `user_session`) there and answers 403 to a generation-bound
one.

Supplied keys must contain 1 to 128 characters, and every key arrives with
`Idempotency-Scope: principal-v1`: a key without the header, or with another
scope value, fails 422 before durable writes. Missing keys on an optional-key
route retain that route's one-shot behavior without an ambiguous-retry
guarantee.

The stored key namespaces the verified principal, logical method/path, and
caller key (`stored_key`, owned by `base.api_contracts.idempotency`); it is a
bounded digest, never a credential, and no raw caller-key namespace exists, so
a caller cannot address another principal's stored reply by submitting a
computed key. Unverified principals fail before claiming keys or writing
inbounds. Delivery and reconciliation of a chat message share one cluster-wide
identity (`chat_message_key`); every other route uses its own method and path.

## MCP credentials

MCP send accepts an optional idempotency_key and ALWAYS derives its namespace
from the already validated MCP client row ID. No opt-out header or global-key
fallback exists for MCP credentials. Same key/body replays the same receipt;
changed body conflicts. Revoked or unauthenticated clients fail authentication
before key lookup. No new credential store, per-tool privilege boundary, or
authorization policy is introduced. Provenance remains separate from credential
identity.
