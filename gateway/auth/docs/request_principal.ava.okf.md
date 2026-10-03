---
type: doc
title: "Credential-Scoped Idempotency"
description: "Server-bound credential principals and explicit versioned idempotency storage namespaces, independent of caller provenance."
tags:
- gateway
- identity
---

# Credential-scoped idempotency

The authentication middleware binds an `AuthPrincipal` only after successful
credential validation. A cluster bearer is one shared cluster principal, not a
different principal per tool label. Validated browser sessions authenticate that
same stable cluster administrator, but only while the credential that minted
them is current ([[web-sessions.ava.okf.md]]); rotating a cookie does not
change principal.
No-auth mode has no verified principal. Caller/source JSON cannot bind one.

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
one. An open API passes, having no credential to tell apart.

Requests explicitly choosing `Idempotency-Scope: principal-v1` namespace their
key by verified principal, logical method/path, and caller key. The stored key
is a bounded digest, never a credential. Delivery and reconciliation use the
same logical message POST path. Unverified principals and unknown scope versions
fail before claiming keys or writing inbounds.

Legacy REST callers retain raw-key behavior to preserve in-flight retries across an
upgrade. The `principal-v1:` storage prefix is reserved and rejected as a raw
legacy key, so a caller cannot bypass scoping by submitting a computed stored
key. Existing callers that used this newly reserved prefix must choose a new
logical request key; do not blindly retry an already committed side effect.

## Rollout boundary

This server-side opt-in does not silently switch existing clients or repair
historical global keys. Clients must positively negotiate this version before
opting in; an old server can ignore an unknown HTTP header. Negotiated client
activation is follow-up integration work. MCP send accepts an optional
idempotency_key and ALWAYS derives its namespace from the already validated
MCP client row ID. No opt-out header or global-key fallback exists for MCP
credentials. Same key/body replays the same receipt; changed body conflicts.
Revoked or unauthenticated clients fail authentication before key lookup.
No new credential store, per-tool privilege boundary, or authorization policy
is introduced. Provenance remains separate from credential identity.
