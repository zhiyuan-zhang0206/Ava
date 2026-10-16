---
type: doc
title: Error responses and middleware order
description: The typed 500 envelope, CORS header placement for unhandled errors, and the middleware order that keeps auth responses readable to an allowed origin.
tags: [gateway]
---

# Error responses and middleware order

```ava-contract
id = "error-response-cors"
kind = "invariant"
statement = "Unhandled errors preserve the typed 500 envelope and reflect credentials CORS headers only for the app's allowed request origins. Auth short-circuit responses also remain readable to an allowed browser origin."
code = ["gateway/http/middleware/error_handlers.py:cors_headers", "gateway/app.py:create_app"]
tests = ["gateway/tests/test_error_envelope.py::test_global_handlers_emit_typed_envelopes", "gateway/tests/bootstrap/test_auth_enforcement.py::test_cors_headers_reflect_allowed_origin", "gateway/tests/bootstrap/test_auth_enforcement.py::test_cors_headers_omit_disallowed_origin", "gateway/tests/bootstrap/test_auth_enforcement.py::test_unauthorized_response_carries_cors_headers"]
```

Starlette places `ServerErrorMiddleware` outside user middleware and assigns the
catch-all exception handler to that outer layer. An unhandled 500 therefore
cannot rely on the inner `CORSMiddleware` to decorate its response. The catch-all
handler explicitly adds `cors_headers(request)`, using the same app-owned exact
origin allowlist; otherwise a browser reports an opaque network failure instead
of the actual typed error. A disallowed origin receives no such headers.
Within user middleware, CORS surrounds auth so an early authentication response
can still be read by an allowed browser client. The request-trace layer supplies
the error envelope's correlation identity.

```ava-contract
id = "test-credential-isolation"
kind = "invariant"
statement = "Task-assignment authentication tests use fixed isolated test credentials, including a deliberately rotated credential. A previously accepted request cannot replay under the revoked credential, and the denied replay creates no additional business rows. These literals are fixture input, not deployed secrets."
code = ["gateway/tests/lifecycle/test_task_assignments.py:<module>", "gateway/tests/lifecycle/test_task_assignments.py:test_revoked_principals_cannot_replay"]
tests = ["gateway/tests/lifecycle/test_task_assignments.py::test_revoked_principals_cannot_replay"]
```

## Relationship to Other Nodes

- [[gateway/docs/gateway.ava.okf.md|Gateway]] — the domain node whose error surface this describes.
