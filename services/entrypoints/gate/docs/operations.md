# Optional HTTPS browser entry (HTTP/2)

Use a browser-facing HTTPS reverse proxy when several visible console windows
exhaust HTTP/1.1's six connections with SSE. Keep `AVA_GATEWAY_URL` and the
existing listener ports as the runner/control-plane addresses. On the gateway
unit, set `AVA_BROWSER_ORIGIN=https://<entry-host>` (an origin only, no path).
The normal frontend build derives its browser setting from this field; do not
write `NEXT_PUBLIC_*` overrides. Deploy the change through the normal update
path so the gateway, gate login page and rebuilt frontend agree. If an explicit
`AVA_GATEWAY_CORS_ALLOWED_ORIGINS` list exists, add the exact new origin there;
the explicit allowlist remains authoritative.

This setting currently requires a source-built frontend. Retained standalone
images do not record a browser origin in their build manifest; their launcher
refuses a nonempty setting instead of silently serving a mismatched bundle.

Only a browser visiting that exact origin uses same-origin API/SSE. Direct IP
frontend URLs keep their existing gateway-port routing for staged verification
and rollback. Login through the new HTTPS origin sets a Secure session cookie;
direct HTTP login retains its existing policy. A hostname change requires a
new browser login. Forward the original Host and overwrite X-Forwarded-Host /
X-Forwarded-Proto at the trusted entry; Uvicorn's trusted proxy boundary stays
loopback, never `forwarded-allow-ips=*`.

Route `/api` and its descendants, `/pages` and its descendants, and `/grafana`
and its descendants directly to the gateway; all remaining requests go to the
existing gate. The gate buffers frontend
responses and must not proxy SSE. Preserve route prefixes, query strings,
cookies, redirects, and immediate `text/event-stream` delivery. Keep the entry
supervised independently of rollout service teardown.

If the host's private network already provides a managed HTTPS entry (listener,
certificate renewal and persistence), it can take this role without a second
proxy daemon. Keep that entry reachable only on the private network, never
publicly exposed. If its certificates come from a public CA, issuance
publishes the node's full DNS name in Certificate Transparency. Save the
entry's current configuration first and refuse to overwrite an occupied HTTPS
port. For example, for gateway 20016 and gate 20017 on an otherwise unused
HTTPS port 443, add exactly four handlers:

| Path on `https://<entry-host>` | Upstream |
|---|---|
| `/api` | `http://127.0.0.1:20016/api` |
| `/pages` | `http://127.0.0.1:20016/pages` |
| `/grafana` | `http://127.0.0.1:20016/grafana` |
| `/` (everything else) | `http://127.0.0.1:20017` |

An entry that strips the mount prefix needs the upstream URL to restore it, as
above. Make the handlers persist across restarts of the entry. Preserve its
other handlers and TCP relays; never reset the whole entry to roll this back.
Remove only the four added handlers. The direct IP entry remains available
throughout.

Acceptance: verify ALPN negotiates `h2` from the user's machine with normal
certificate verification; load several console windows and inspect the real
browser Network protocol and queueing; check login, tree, inspector, SSE,
Grafana/pages and maintenance-state requests. API headers alone are not proof
that the browser used HTTP/2. Full test suites run in CI; local verification
uses the affected tests and the operator's authorized browser smoke.
