---
type: doc
title: "Asserted External Caller Identity"
description: "Bounded external caller provenance, structured inbound and audit persistence, and reader-first rollout constraints."
tags:
- shared
- identity
---

# Asserted external caller identity

`CallerIdentity` is the bounded external/unknown provenance value object. It is
not an authenticated principal, capability, transport, request ID, or Ava agent
identity. Unknown fields and malformed identifiers are rejected. No secrets or
human identity belong in an instance identifier.

`external_agent:codex:run-42` and `unknown:legacy` are display projections.
`shared.envelope` reads them as explicitly asserted external or unknown callers,
never as User, Ava Agent, or system. Existing source formats remain readable.

Chat and lifecycle inbound writes persist the parsed object in the existing
JSONB `payload.caller_identity` sidecar. Audit events carry the same reserved
structured attribute. Conflicting or malformed reserved metadata is rejected
before writing, other payload fields are preserved, and legacy source values
receive no inferred identity. Chat reconciliation normalizes this sidecar in
the same way as insertion, so retries compare the same immutable payload.

## Rollout boundary

Chat writes use the same-transaction generation/owner/fresh-lease gate in
`caller_protocol.py`, including manual and direct internal callers. Lifecycle
and bootstrap writes remain blanket-fenced until their durable admission
handoff exists. Production admission still advertises protocol 0, so this does
not activate new-format producers. Storage-only tests replace the gate explicitly;
the separate integration proof uses real hosted admission and claim helpers.
Older binaries reject new formats, so protocol activation additionally requires
the independently verified old-writer upgrade barrier.
Never wrap external provenance in
`system:*` or `agent:*` to bypass an old validator. A caller field accepted by an
HTTP schema but discarded before persistence is not structured audit storage.

Subsequent integration must propagate explicit provenance from CLI/MCP/SDK.
Producers must fail clearly against incompatible consumers. Shared cluster
credentials cannot prove the caller label: authorization and idempotency scopes
must derive from actual server-bound credentials, not this object.
