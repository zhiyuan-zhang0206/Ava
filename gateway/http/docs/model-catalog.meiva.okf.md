---
type: doc
title: Gateway model catalog failure boundary
description: An atomic model catalog or retained startup failure separates model operations from management and recovery.
tags: [gateway, lm]
---

# Gateway model catalog failure boundary

The gateway builds its model catalog once per lifespan. The provider loader still
fails on a broken enabled provider, invalid model declaration or empty provider
set. It never skips a provider or publishes the portion loaded before failure.
The app retains either the complete catalog or the original exception. A provider
may supply the only model required by a workload; a partial catalog cannot prove
that the workload is valid.

```meiva-contract
id = "retained-catalog-failure"
kind = "invariant"
statement = "A catalog build failure retains the original Exception and traceback without publishing a partial catalog. Each model-dependent access raises a fresh ModelCatalogUnavailableError chained to that original cause. Process-exit exceptions propagate."
code = ["gateway/http/model_catalog.py:load_model_catalog", "gateway/http/model_catalog.py:require_catalog"]
tests = ["gateway/tests/bootstrap/catalog/test_provider_startup.py::test_catalog_failure_preserves_identity_and_traceback", "gateway/tests/bootstrap/catalog/test_provider_startup.py::test_process_exit_does_not_become_model_unavailable"]
```

Only catalog construction has this boundary. Invalid core configuration, database
startup and authentication dependencies retain their own failure behavior.
`require_model_catalog` reads the serving app's result; it does not rebuild,
retry, substitute an empty catalog, or consult a second global owner. The original
failure is logged with its traceback at startup. HTTP consumers receive 503 with
`code=model_catalog_unavailable`, a repair instruction and `retryable=false`;
internal provider diagnostics do not appear in the response.

Authentication, ordinary configuration editing, user settings, inventory
management, text delivery and model-independent history/lifecycle operations do
not acquire a catalog merely to start the gateway. Model selection, creation,
bootstrap's provider-key projection, resolved configuration, image capability
checks, memory search, context budgets and understanding operations require it.
Agent directory, roster and detail preserve database facts while their required
`supports_vision` field becomes null. The app's retained catalog result selects
this read-only capability mode; there is no second catalog owner. Resolution and
validation are owned by [[base/agents/observation/docs/observation.meiva.okf.md]].
Health remains process liveness, not an assertion that model operations work.

Reply suggestions and HTTP MCP tools receive a catalog reader so their
construction does not force model availability. Their request boundaries are
owned by [[gateway/agents/docs/reply-suggestion/reply-suggestion.meiva.okf.md]] and
[[gateway/mcp_server/docs/mcp-endpoint/mcp-endpoint.meiva.okf.md]]. Existing task-assignment
receipts are returned before model-dependent launch observation; a launch
failure cannot erase accepted work.

Repair provider code/configuration or the enabled set, then restart the gateway
to build a new atomic catalog. No hot catalog reload, plugin disable heuristic,
service-roster deletion or automatic model fallback is implied.

Integration coverage lives in `gateway/tests/bootstrap/catalog/test_provider_startup.py`,
`test_model_failure_routes.py`, `gateway/tests/bootstrap/catalog/test_mcp_model_failure.py`
and `gateway/tests/lifecycle/test_task_assignments.py`. These tests use isolated
homes and database fixtures; they do not establish a deployed cluster's health.
