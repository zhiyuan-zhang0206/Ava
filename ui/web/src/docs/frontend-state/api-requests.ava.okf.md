---
type: doc
title: Browser API requests
description: Behavioral constraints retained alongside their implementation and tests.
tags: [frontend]
---

# Browser API requests

```ava-contract
id = "request-budget"
kind = "invariant"
statement = "The timeout and caller cancellation remain connected through both response headers and response-body consumption. The Inspector client budget leaves serialization and network headroom above the backend work budget. HTTP rejection retains its status separately from network failure. Cross-origin gateway requests include session cookies. Slow-request telemetry removes query strings and normalizes numeric IDs, and telemetry requests do not report themselves."
code = ["ui/web/src/lib/transport/api.ts:<module>"]
tests = ["ui/web/src/lib/transport/api.test.ts::agent label / messages / cancel::times out when headers arrive but the response body never completes", "ui/web/src/lib/transport/api.test.ts::run timeline read ownership::keeps caller cancellation connected while consuming the response body", "ui/web/src/lib/transport/api.test.ts::browser API timing::reports a slow request with query strings removed and numeric segments collapsed"]
```

## Test boundary

Endpoint-shape cases assert the gateway path, method and payload rather than a particular test host. These cases do not establish runtime response-schema validation: generated OpenAPI types describe success payloads, while the transport preserves HTTP failures and decodes JSON. The upload fixture is constructible and exposes its initialized instance before the caller installs event handlers, so progress, response and error events can be driven independently.

```ava-contract
id = "upload-transport-fixture"
kind = "invariant"
statement = "The XMLHttpRequest test constructor returns a fully initialized instance and exposes that same instance before uploadFiles installs its handlers. Tests drive progress, load and failure callbacks on the instance used by the request."
code = ["ui/web/src/lib/transport/api.test.ts:FakeXHR"]
tests = ["ui/web/src/lib/transport/api.test.ts::uploadFiles::POSTs one file as FormData to /api/agents/{id}/uploads under a principal-v1 key", "ui/web/src/lib/transport/api.test.ts::uploadFiles::calls onProgress when lengthComputable", "ui/web/src/lib/transport/api.test.ts::uploadFiles::rejects on network error"]
```
