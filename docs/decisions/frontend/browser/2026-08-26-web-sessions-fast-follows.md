# Web-session list masking and managed-browser label

## Context

The server-side web-sessions change (2026-08-25) exposed full session ids to
any authenticated caller of `GET /api/auth/sessions`. That is inside the
cluster-trust boundary, but the ids of sessions the caller does not hold are
still credentials of a kind: a leaked list response leaks every active
session id, and a session id is all the revoke endpoint needs. The managed
browser also logged in indistinguishably from a human browser, so its rows
could not be told apart in the list and were easy to revoke by accident; a
revoked managed session stayed broken until the daemon's next 6-hour refresh
tick.

## Decision

The sessions list masks every non-current row's id to its final 8 characters
(the full id remains only on the row the request itself holds). The revoke
endpoint accepts the suffix, so masking costs no revocability: an exact-id
revoke is tried first, then a suffix match against active rows — a suffix
shared by more than one active session is refused with 409 rather than
revoked wholesale. The suffix fallback only triggers for the exact 8-character
masked form (a longer string is a full id and 404s when unknown) and never
matches the request's own session, whose only revocation path is logout
(QA guard-bypass follow-up, 2026-08-27).

Managed-browser sessions are labeled: the browser daemon's gateway login sends
a dedicated user agent (`ava-managed-browser`), and the list derives
`managed: true` from it. No schema change; the label is a stored fact already
captured at login.

The daemon additionally re-checks its stored cookie against
`/api/auth/check` after any navigation to a gateway-origin URL and re-injects
immediately when the session no longer authenticates, so an accidentally
revoked (or expired) managed session heals on the next gateway page instead of
waiting out the 6-hour refresh interval. The scheduled refresh stays as the
safety net.

## Alternatives rejected

Returning a separate revoke token per row was rejected: the masked suffix
already serves as the revoke handle, and a second identifier adds surface
without a threat it answers. Detecting the 401 inside the page itself (CDP
network events) was rejected as too much machinery for a rare, self-healing
condition.
