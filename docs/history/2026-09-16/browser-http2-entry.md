# An optional browser entry for HTTP/2

The console opens three SSE streams per visible page. Multiple visible pages
can consume a browser's HTTP/1.1 connection pool and queue unrelated REST
requests. Direct API probes can therefore remain fast while the browser is
slow. This transport bottleneck is separate from historical inspector query
cost and the terminated-agent roster payload.

The chosen boundary is the browser-facing proxy. It can terminate HTTPS and
HTTP/2 while the existing gateway continues to speak HTTP/1.1 upstream. Runner
enrollment, data-plane addresses and API payloads do not migrate. An explicit
browser origin selects same-origin API/SSE only for visits to that origin;
the direct IP entry remains available for an incremental operator cutover.

Replacing Uvicorn, combining the event protocols, and electing a cross-window
SSE leader were not selected: each couples a browser transport fix to a larger
runtime or event-state change. The existing Tailscale daemon can provide a
persistent private HTTPS proxy and certificate renewal without adding a new
application dependency. Certificate enablement and the actual browser rollout
remain deployment operations, not side effects of setting the origin.

The gate remains the frontend and maintenance entry. SSE bypasses its buffered
proxy. Login, CSP and API selection must agree on the external origin, including
removing an internal port when a forwarded host uses the default HTTPS port.
Tests cover that boundary and preserve direct HTTP login and explicit CORS
policy. Production acceptance requires the user's browser to negotiate h2 and
remain responsive with multiple SSE consumers.

Release-path review: the retained standalone image contract currently records
only the gateway port and API-base override. Its launcher rejects this new
setting until the manifest can represent it; source builds carry the setting
through the canonical build command. Silently accepting an unrepresented
origin would break HTTPS clients despite successful release verification.
