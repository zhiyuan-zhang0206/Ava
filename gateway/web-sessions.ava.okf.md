---
type: doc
title: Gateway — web sessions & browser-origin policy
description: Server-side session and bearer-secret auth for the gateway API — web_sessions rows, TTL refresh, session listing/revocation, exact-origin CORS checks, and the Secure cookie policy.
tags: []
---

# Gateway — web sessions & browser-origin policy

- **Authentication and browser-origin policy**: with a cluster secret, `/api/*` requires an opaque server-side session or bearer secret, except health and browser login/check/logout; auth can be disabled for tests. Login creates a TTL-bounded `web_sessions` row; middleware caches positive checks for 30 seconds and touches recent use once per minute. Active sessions can be listed, non-current ones revoked, and logout revokes the current one. Cookie-authenticated mutations with an `Origin` require an exact CORS allowlist match; bearer and originless callers are unaffected. Cookie `Secure` is explicit or derived from `gateway_url`. An EMPTY secret is the unauthenticated, loopback-only posture; `/api/bootstrap` registers agent-runners.
- **CORS allowlist** (`gateway/_cors.py`): exact origins, credentials allowed, never a wildcard. A non-empty `AVA_GATEWAY_CORS_ALLOWED_ORIGINS` is authoritative and used verbatim. Empty derives: `localhost` / `127.0.0.1` at the Gate entry port; `localhost` / `127.0.0.1` at this home's reserved Next.js app port (`AVA_APP_PORT`, written from the registry record at start; unset derives no app origin); `AVA_BROWSER_ORIGIN`; and the gateway URL's own origin plus the entry port on its host. The app origins are loopback-only because Next.js binds `127.0.0.1` only, so no `[::1]` or remote form exists. They apply whether or not Gate is selected: Gate proxies the same listener under the allowed entry origin, so the app's own origin trusts no additional content, and a browser can use the app directly when Gate is not running (the local branch preview).

Parent node: [[gateway.ava.okf.md|Gateway]].
