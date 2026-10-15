---
type: doc
title: Gateway fixture evidence
description: Authentication and application lifespan boundaries in gateway tests.
---

# Gateway fixture evidence

`gateway/tests/agents/gateway_auth.py` injects credentials only into an assembled
app whose state contains `GatewayConfig`. Explicit Authorization, including an
empty value, wins. Requests still pass through production authentication
middleware; authentication-specific tests opt out of automatic credentials.

Lifespan-owned concurrency gates are rebuilt for each test app so a semaphore
bound to an earlier event loop cannot leak into the next test. Upload pool state
must be assembled in the real lifespan order. Use the `memory_search_app` and
`gateway_test_client` owners: a bare client without startup does not establish
that application-state contract.
