# TLS + Authentication for Ava Gateway & Frontend

> **Status: Phases 1 + 2 landed and deployed. Phase 3 (TLS) is the only remaining
> slice.**
>
> Phase 1 (the gateway auth middleware) merged: every protected route requires
> a session cookie or a Bearer cluster secret when the cluster has one; an empty
> secret is the supported loopback-only single-box posture. Phase 2 shipped as
> **cookie-based session auth**, not
> the Next.js proxy this doc originally recommended (see "Decision" below).
>
> Current edge posture (bearer-gated when configured, loopback + `AVA_MACHINE_HOST`
> binds, trusted CIDRs) is in `AGENTS.md` "Running" + [`runbook.md`](../../conventions/runbook.md).
> Everything below is kept for the *why* — chiefly why cookies beat a proxy and
> beat bearer tokens in the browser.

## Vision

Ava should have proper authentication like any normal web app:
- **HTTPS everywhere** — no plain HTTP on any user-facing surface. *(Phase 3, the
  remaining gap: the browser still talks plain HTTP to `:8000` / `:3000` over the
  private overlay.)*
- **Browser users authenticate with session tokens** — the cluster secret
  never appears in browser JS. ✅
- **Agent-to-agent / backend communication uses the cluster secret** — the
  pre-shared key model. ✅
- **Frontend never sees the cluster secret** — it is a server-side credential. ✅

## Starting point (superseded)

```
Browser ──plain HTTP──▶ :8000 (gateway, unauthenticated)
Browser ──plain HTTP──▶ :3000 (frontend, Next.js)
SDK/agents ──plain HTTP──▶ :8000 (gateway, unauthenticated)
```

No authentication on any endpoint — the private network was the trust boundary. The
cluster secret existed but was only used for data-plane auth (pg/redis passwords).

## Phase 1 — gateway auth middleware (landed)

`Authorization: Bearer <secret>` on every `/api/*` route, constant-time compared
(`shared/cluster_auth.py`: `bearer_header()` / `verify_bearer()`, pure stdlib). The
SDK transport (`ava/_gateway_transport.py`) and `scripts/start_agent.py` inject the header
automatically.

Two things changed versus the original Phase-1 draft:

- **The empty-secret posture is explicit.** With a secret, authentication fails
  closed; without one, a single-box gateway serves unauthenticated and binds
  loopback only. `auth_middleware_enabled=false` remains a separate e2e/test knob.
- **The bypass list is fixed**, not route-prefix based: `/api/health`,
  `/api/auth/{login,check,logout}`, and `/api/alerts` (which applies its own token
  or loopback policy).

It could not merge alone — the middleware would have locked the frontend out
immediately, since browser `fetch()` and `EventSource` carried no auth. That
forcing function is what produced Phase 2.

## Phase 2 — browser-to-gateway auth (deployed as cookies)

### Constraints that drove the choice

1. **`EventSource` cannot send custom headers.** The standard API takes a URL and
   `withCredentials` — there is no way to attach `Authorization: Bearer` to an SSE
   connection. Ava's UI is SSE-heavy, so this is not a corner case.
2. **The cluster secret must stay server-side** — never in browser JS, a
   `NEXT_PUBLIC_*` env var, or the bundle.
3. **Single user, private network.** No OAuth, no multi-user, no federated identity.
4. **Backward compatible** with SDK callers already carrying the secret (Phase 1).
5. **Per-cluster scope** — different clusters have different secrets.

### Decision: cookie-based session auth

`POST /api/auth/login` verifies the password (the cluster secret) and sets an
HttpOnly `ava_session` cookie (`gateway/routers/auth.py`); the browser then carries
it automatically on both `fetch()` and `EventSource`. CORS allows exact configured
origins (or derives the local and gateway-host frontend origins), since the frontend
(`:3000`) and gateway (`:8000`) are co-located but cross-origin. Cookie-authenticated
mutations reject a present origin outside that allowlist. SDK / agent / script callers
keep using Bearer. The cookie `Secure` flag is explicitly configurable and otherwise
follows the configured gateway URL scheme, ready for Phase 3 to switch it on with TLS.

Rationale: this is how every website works — login → cookie → auto-carry. It needs
no custom headers for SSE, no proxy layer, and it is the shape that can go public
later once Phase 3 adds TLS.

### Alternatives rejected

- **Next.js API proxy** (the original recommendation): a catch-all
  `ui/web/src/app/api/[[...path]]/route.ts` forwarding to the gateway with the
  Bearer secret injected server-side, so the browser stays same-origin and never sees
  the secret. Rejected — it exists *only* to work around `EventSource`'s header
  limitation, which cookies solve natively; it puts the Next.js server on the
  critical path for all API traffic (including SSE streaming, which it would have to
  proxy through a `ReadableStream`); and it does not extend to a public deployment
  without further work. No such route exists in the tree.
- **Bearer session tokens in the browser** (JWT / opaque token in
  localStorage): standard, but hits the same `EventSource` wall — every workaround
  is worse than a cookie (cookies-for-the-token reintroduces the cookie path anyway;
  an `EventSource` polyfill adds a dependency; a token in the query string gets
  logged). It also means building login-page + issuance + verification + refresh for
  a single-user app.

## Phase 3 — TLS termination (REMAINING, not built)

The one thing still missing from the vision: browser ↔ gateway and browser ↔
frontend are plain HTTP, relying on the encrypting overlay (WireGuard) for
confidentiality.

- **Option 3a — the VPN overlay's own TLS-termination feature (chosen
  direction).** Several overlays ship a `serve`-style subcommand that fronts a
  local port with a valid Let's Encrypt cert under the node's overlay-assigned
  hostname (`https://<hostname>.<overlay-domain>`), terminated by the overlay's
  own client — e.g. `<overlay-cli> serve --bg --https=443 http://localhost:3000`.
  Zero-config, no cert management, already on the private network. Cost:
  reachable only within that network, and it's specific to whichever overlay the
  operator runs.
- **Option 3b — Caddy reverse proxy** with auto Let's Encrypt. Overkill for a
  private-network-only deployment; keep as the answer if a genuinely public edge is
  ever wanted.

Shape of the work: a converge step that registers the overlay's serve-style
feature where the operator's overlay offers one, made optional (HTTP on
loopback must keep working where it's unavailable). Phase 2
already leaves the seam — the per-request credential is transport-independent, so
TLS is "add a cert + switch the scheme", not a re-architecture.

## Still open

1. **Should loopback be exempt from auth?** Leaning no — keep auth uniform; an
   exemption would reintroduce a "same box" special case.
2. **Should the gateway stop binding `0.0.0.0`?** It should bind `127.0.0.1` AND
   `AVA_MACHINE_HOST`, matching the Postgres/PgBouncer posture; Redis remains
   loopback-only with off-box ingress carried by the relay bridge. The ops server
   deliberately still binds `0.0.0.0` (the gateway dials it across the private
   network and it authenticates), so this is specifically about the gateway's
   own listener.
