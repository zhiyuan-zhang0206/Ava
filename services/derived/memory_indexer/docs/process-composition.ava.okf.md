---
type: doc
title: "Memory-indexer entry configuration ownership"
description: "The prepared entry config and runtime path supply resources and live database handles."
tags:
- services
- configuration
---

# Memory-indexer entry configuration ownership

The service entry retains its prepared `ConfigBoot` and resolved unit runtime
path. Endpoint and pidfile operations borrow those inputs through
`ServiceEndpoints.from_boot`; each original lookup reads the same live view.
The work and logging database factory calls `db_config_from_boot` at each
handle creation and retains the entry's gate and lazy machine host reader.
Schema checks, health-before-pool ordering and shutdown stay at their original
boundaries. Business-specific configuration slices keep their existing readers.
