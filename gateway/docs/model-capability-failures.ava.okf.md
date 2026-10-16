---
type: doc
title: Model capability failures
description: "Catalog unavailability at the gateway: management stays up while model-dependent requests fail explicitly, separate from DB/config/auth validity."
tags: [gateway]
---

# Model capability failures

[[gateway/http/docs/model-catalog.ava.okf.md]] owns catalog availability. An enabled
provider failure leaves the authenticated management surface running; requests
that require model metadata fail explicitly. This is separate from database,
core-configuration and authentication startup validity.

## Relationship to Other Nodes

- [[gateway/docs/gateway.ava.okf.md|Gateway]] — the domain node; catalog ownership is [[gateway/http/docs/model-catalog.ava.okf.md|Model Catalog]].
