---
type: doc
title: Error responses and middleware order
description: The typed 500 envelope, CORS header placement for unhandled errors, and the middleware order that keeps auth responses readable to an allowed origin.
tags: [gateway]
---

# Error responses and middleware order

Unhandled errors preserve the typed 500 envelope and reflect credentials CORS headers only for the app's allowed request origins. Auth short-circuit responses also remain readable to an allowed browser origin.

Starlette places `ServerErrorMiddleware` outside user middleware and assigns the
catch-all exception handler to that outer layer. An unhandled 500 therefore
cannot rely on the inner `CORSMiddleware` to decorate its response. The catch-all
handler explicitly adds `cors_headers(request)`, using the same app-owned exact
origin allowlist; otherwise a browser reports an opaque network failure instead
of the actual typed error. A disallowed origin receives no such headers.
Within user middleware, CORS surrounds auth so an early authentication response
can still be read by an allowed browser client. The request-trace layer supplies
the error envelope's correlation identity.

Task-assignment authentication tests use fixed isolated test credentials, including a deliberately rotated credential. A previously accepted request cannot replay under the revoked credential, and the denied replay creates no additional business rows. These literals are fixture input, not deployed secrets.

## Relationship to Other Nodes

- [[gateway/docs/gateway.meiva.okf.md|Gateway]] — the domain node whose error surface this describes.
