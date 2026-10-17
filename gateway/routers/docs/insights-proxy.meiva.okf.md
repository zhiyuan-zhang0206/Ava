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
