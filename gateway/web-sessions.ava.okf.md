---
type: doc
title: Gateway — web sessions & browser-origin policy
description: Server-side session and bearer-secret auth for the gateway API — web_sessions rows, TTL refresh, session listing/revocation, exact-origin CORS checks, and the Secure cookie policy.
tags: []
---

# Gateway — web sessions & browser-origin policy

- **Authentication and browser-origin policy**: with a cluster secret, `/api/*` requires an opaque server-side session or bearer secret, except health and browser login/check/logout; auth can be disabled for tests. Login creates a TTL-bounded `web_sessions` row; middleware caches positive checks for 30 seconds and touches recent use once per minute. Active sessions can be listed, non-current ones revoked, and logout revokes the current one. Cookie-authenticated mutations with an `Origin` require an exact CORS allowlist match; bearer and originless callers are unaffected. Cookie `Secure` is explicit or derived from `gateway_url`. An EMPTY secret is the unauthenticated, loopback-only posture; `/api/bootstrap` registers agent-runners.

Parent node: [[gateway.ava.okf.md|Gateway]].
