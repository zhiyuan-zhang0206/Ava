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

Loop tuning is a frozen `MemoryIndexerConfig` (`loop_interval_s`,
`patrol_interval_s`, `retry_backoff_s`, `retry_limit`, `excluded_basenames`,
`reconcile_chunk_paths`, `batch_size`; defaults are the production values).
`main` constructs it and passes it to `run`, which hands it to the patrol loop,
each pass (`reconcile`) and `process_paths`. No module constant holds these
values; a test that needs a small batch or a short interval constructs its own
config.
