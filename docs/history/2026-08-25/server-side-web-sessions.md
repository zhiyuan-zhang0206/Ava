# Server-side web sessions

## Context

Browser authentication needed a revocation boundary narrower than the cluster
secret. A self-contained HMAC cookie could be checked without storage, but a
leaked cookie remained usable until its long expiry. Invalidating it early
required rotating the cluster secret, which also invalidated every browser and
every service-to-service bearer credential in the cluster.

The managed Chrome service also derived that cookie locally from the cluster
secret. That bypassed the login boundary and made the browser credential's
lifecycle inseparable from the bearer secret rather than from a login session.

## Decision

Browser cookies carry opaque 256-bit random identifiers. PostgreSQL owns their
creation time, expiry, revocation, recent-use metadata, user agent, and source
address. The gateway permits a positive validation cache for at most 30 seconds
and never beyond the row's own expiry; local revocation evicts the cache
immediately. Browser-visible session management lists active sessions and can
revoke any non-current one, while logout revokes the current session.

The default lifetime is one day and remains configurable independently of the
settings-free cookie helper. Managed Chrome now authenticates through the same
login endpoint as any browser and refreshes its returned cookie every six hours.

Keeping stateless HMAC cookies and adding only a shorter expiry was rejected:
it reduced exposure but still could not revoke one leaked credential. Rotating
the cluster secret on logout or suspected leakage was rejected because its
blast radius includes unrelated browser sessions and machine credentials.

Existing HMAC cookies intentionally do not migrate; users authenticate again
after this change.
