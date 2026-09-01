# Nonce CSP browser-origin boundary

## Decision

The frontend emits a request-unique nonce Content Security Policy from the
Next.js 16 proxy entry point rather than a static `next.config.ts` header. The
proxy sends the policy on the forwarded rendering request and on the browser
response, which lets Next.js nonce its framework scripts while the browser
enforces the same policy.

In production, script and style elements require the nonce; `style-src-attr`
is the explicit compatibility exception for existing dynamic layout attributes.
It does not relax `script-src`, which retains no unsafe directive. Development
retains the Next.js evaluation and inline-style allowances. `connect-src` names
only the frontend, the resolved gateway HTTP origin, and its WebSocket origin.
`frame-src` permits only same-origin Grafana through `/grafana`.

## Rationale

The public frontend host can differ from the loopback address used by the gate
to reach Next.js. The gate preserves the browser `Host` as well as
`X-Forwarded-Host` and `X-Forwarded-Proto`; the CSP proxy uses them to recover
the browser-facing origin when no explicit `NEXT_PUBLIC_API_BASE` is configured.
A TLS terminator must overwrite the two forwarding headers before it reaches
the gate. The gate also relays CSP and the static browser-security headers on
the public response.
