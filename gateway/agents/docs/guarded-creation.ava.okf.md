---
type: doc
title: Guarded Agent Creation
description: Versioned plain-creation admission and receipt compatibility during mixed gateway generations.
tags:
- gateway
- agents
---

# Guarded Agent Creation

`POST /api/keyed/v1/agents` accepts plain creation through the existing birth
transaction and launch owner. Every request requires a valid `Idempotency-Key`
(1–128 characters), the exact `Idempotency-Scope: principal-v1` header, and a
verified authenticated principal. Missing or invalid admission, an unverified
principal, and `fork_from` are rejected before birth or launch effects. Existing
authentication still applies to replay; revoked credentials cannot retrieve a
receipt.

The receipt identity includes the principal, POST method, and this versioned
logical path. Identical keyed requests replay the committed birth; changed
request data returns 409. Acceptance and launch recovery retain the semantics
in [[agents-router.ava.okf.md]]; acceptance does not prove native execution.

An older gateway has no route for this versioned POST and rejects it without
creating an agent. A future strong client must keep the same path, key, and
body for every attempt of one intent. It must never downgrade that intent to
`POST /api/agents`: the legacy and guarded paths have distinct receipt
namespaces, and an older gateway may ignore a key on the legacy path. A cached
capability GET or observed generation cannot prove the backend serving a later
write supports keyed admission.

The default creation helper namespace remains `/api/agents`, including MCP's
existing canonical principal-scoped keys. Existing HTTP and MCP requests keep
their identities across this addition. This server-only change does not
activate clients, add capability discovery, or change the legacy route's
conservative retry gate. The guarded route declares transactional keyed
idempotency while keeping automatic legacy keyed retries disabled. SDK strong
mode and additional creation surfaces remain follow-up work.
