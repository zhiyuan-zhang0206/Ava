---
type: doc
title: Browser API requests
description: Behavioral constraints retained alongside their implementation and tests.
tags: [frontend]
---

# Browser API requests

## Test boundary

Endpoint-shape cases assert the gateway path, method and payload rather than a particular test host. These cases do not establish runtime response-schema validation: generated OpenAPI types describe success payloads, while the transport preserves HTTP failures and decodes JSON. The upload fixture is constructible and exposes its initialized instance before the caller installs event handlers, so progress, response and error events can be driven independently.
