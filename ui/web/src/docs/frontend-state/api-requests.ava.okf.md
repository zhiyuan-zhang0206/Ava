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
