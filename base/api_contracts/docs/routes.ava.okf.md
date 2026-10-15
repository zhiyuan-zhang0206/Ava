---
type: doc
title: Route retry and pause contracts
description: Behavioral constraints retained alongside their implementation and tests.
tags: [api]
---

# Route retry and pause contracts

```ava-contract
id = "route-policy"
kind = "invariant"
statement = "Route contracts are the common retry and pause policy owner. Unknown routes are non-idempotent. Keyed receipts establish acceptance, not downstream delivery or execution. Pause exemptions match both method and path: alert webhook writes remain available while ordinary alert reads pause. Bootstrap and control-plane routes remain available so operators can inspect or recover a paused cluster."
code = ["base/api_contracts/contracts.py:<module>"]
tests = ["base/api_contracts/tests/test_route_contracts.py::test_unknown_route_defaults_to_non_idempotent", "base/api_contracts/tests/test_route_contracts.py::test_sdk_inherits_idempotency_from_contracts", "gateway/http/middleware/tests/test_route_contracts_middleware.py::test_pause_exempt_surface_is_audited"]
```
