---
type: doc
title: Host primitives
description: Operating-system integration, environment ownership and local host utilities.
tags: [base]
---

# Host primitives

`base/host/` owns operating-system integration, environment ownership and local host utilities.
Its component nodes describe the current contracts and implementation.

## Documented components

- [[base/host/env/docs/audit.ava.okf.md]] — Base — .env write audit & integrity guard.
- [[base/host/env/docs/registry.ava.okf.md]] — Environment declarations and boot/child projections.

`base/host/net/resilience.py` owns immutable `Policy` parameters and the shared
`retry` / `aretry` executors, including backoff, jitter, classification and
idempotency gates. Bootstrap fetches use this owner. Provider and Redis-listener
loops still have dedicated contracts; their remaining audit is a
[future item](../../../future/infra/engineering/retry-consumer-contracts.md).
