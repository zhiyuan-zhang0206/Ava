---
type: doc
title: Insights proxy contract
description: Insights proxy contract.
tags: [contracts]
---

# Insights proxy contract

The authenticated Gateway owns browser credentials and isolation admission.
Insights owns the read model and performs no authentication on its private Unix
socket. The shared upstream client belongs to Gateway lifespan. Forward paths
and query strings unchanged, with only the explicitly allowed request/response
headers. Never forward browser cookies or credentials. Responses are finite
buffered JSON. Connection failures are 502 and timeouts 504; other upstream
statuses and bodies pass through. Typed query declarations keep OpenAPI aligned
with the service, including bounded windows and snapshot identities. Underscored
Python parameters use explicit public query aliases because only the untouched
query string is forwarded. Untyped rest paths reject traversal.

```meiva-contract
id = "insights-proxy-boundary"
kind = "invariant"
statement = "Proxying preserves public paths, queries and upstream statuses, strips browser credentials, rejects isolated callers and keeps gateway/service declarations aligned."
code = ["gateway/routers/insights.py:<module>"]
tests = ["gateway/tests/routers/proxies/test_insights_proxy.py::test_a_typed_route_forwards_its_path_and_query_untouched", "gateway/tests/routers/proxies/test_insights_proxy.py::test_the_browsers_credentials_are_not_forwarded", "gateway/tests/routers/proxies/test_insights_proxy.py::test_an_eval_isolated_caller_is_refused_before_the_service_is_asked", "gateway/tests/routers/proxies/test_insights_proxy.py::test_the_gateway_declares_exactly_what_the_service_serves"]
```
