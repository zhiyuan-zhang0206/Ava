---
type: doc
title: Gateway — web sessions & browser-origin policy
description: Server-side session and bearer-secret auth for the gateway API — web_sessions rows, TTL refresh, session listing/revocation, exact-origin CORS checks, and the Secure cookie policy.
tags: []
---

# Gateway — web sessions & browser-origin policy

- **Authentication and browser-origin policy**: with a cluster secret, `/api/*` requires an opaque server-side session or bearer secret, except health and browser login/check/logout; auth can be disabled for tests. Login creates a TTL-bounded `web_sessions` row whose id `<mint>.<random>` names the credential that logged the browser in: `human-<mac>` for the human secret, `runner-<mac>` for the active write generation's runner API token (a unit's managed browser). The mac is HMAC-SHA256 under the gateway's private `$AVA_HOME/web-session.key` (0600, minted at the first login) over the credential's digest, so a cookie gives no offline handle on the credential. A session authenticates only while its mint belongs to a current credential (`gateway.http.auth.request_principal.session_mints`): a rotated human secret or a lost session key ends the sessions it bound at once, with no revocation step, and an id without a mint never authenticates. The principal's credential fact is `user_session` or `machine_session:runner`. Middleware caches positive checks for 30 seconds (after the mint check) and touches recent use once per minute. Sessions that still authenticate (by the same mint test) can be listed, non-current ones revoked, and logout revokes the current one. Cookie-authenticated mutations with an `Origin` require an exact CORS allowlist match; bearer and originless callers are unaffected. Cookie `Secure` is explicit or derived from `gateway_url`. An EMPTY secret is the unauthenticated, loopback-only posture; `/api/bootstrap` registers agent-runners.
- **CORS allowlist** (`gateway/http/auth/cors.py`): exact origins, credentials allowed, never a wildcard. A non-empty `AVA_GATEWAY_CORS_ALLOWED_ORIGINS` is authoritative and used verbatim. Empty derives: `localhost` / `127.0.0.1` at the Gate entry port; `localhost` / `127.0.0.1` at this home's reserved Next.js app port (`AVA_APP_PORT`, written from the registry record at start; unset derives no app origin); `AVA_BROWSER_ORIGIN`; and the gateway URL's own origin plus the entry port on its host. The app origins are loopback-only because Next.js binds `127.0.0.1` only, so no `[::1]` or remote form exists. They apply whether or not Gate is selected: Gate proxies the same listener under the allowed entry origin, so the app's own origin trusts no additional content, and a browser can use the app directly when Gate is not running.

Parent node: [[gateway.ava.okf.md|Gateway]].

The gateway lifespan owns authentication rejection counters and client/path warning
throttles. Middleware and its aggregate telemetry flusher share that instance; a
new application lifespan starts with fresh counters and warning budgets.

The lifespan also owns one login failure limiter shared by all of its requests.
Per-IP lockout, Retry-After, expiry, successful-login reset, and bounded eviction
retain the configured policy; separate application lifespans share no counters.
