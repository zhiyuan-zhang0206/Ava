# Gateway browser-origin policy

The security audit found three coupled browser-boundary gaps: credentialed
CORS reflected every origin, session-cookie mutations did not validate Origin,
and login inferred the cookie's `Secure` flag from the inbound request scheme
even though the deployment topology can terminate transport before the gateway.

The gateway now has one exact-origin policy shared by CORS and cookie-authenticated
state changes. An explicit comma-separated allowlist is authoritative. When it is
empty, the gateway derives localhost, `127.0.0.1`, and the configured gateway host
at the frontend entry port. A cookie-authenticated POST, PUT, PATCH, or DELETE with
a non-allowlisted Origin fails closed; bearer and legacy header credentials are not
ambient browser credentials, and clients without Origin remain supported.

Session-cookie transport policy is also gateway configuration. An explicit boolean
wins; otherwise the `Secure` flag follows the configured gateway URL scheme rather
than the request observed by the application.

Keeping wildcard credentialed CORS was rejected because private reachability does
not make an arbitrary browser origin trusted. Rejecting requests without Origin was
rejected because non-browser clients commonly omit it. Request-scheme inference was
rejected because it describes the final application hop, not necessarily the user's
transport.
