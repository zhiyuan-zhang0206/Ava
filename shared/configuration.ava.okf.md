---
type: doc
title: Configuration and Bootstrap
description: 'Runtime Settings, field metadata, local env ownership, and bootstrap distribution.'
tags:
- configuration
- infrastructure
---

# Configuration and Bootstrap

`shared/config/` defines the per-domain Pydantic settings models and the public
`settings` facade. Normal runtime import constructs the singleton and preserves
its fail-fast configuration contract. Existing settings-lite
`AVA_CONFIG_FETCH=skip` verbs defer construction until first attribute access,
so metadata-only repair code can load model declarations without reading a
broken local `.env` or fetching runner configuration.

`shared/config_registry.py` is the single projection of field aliases,
annotations, editor types, choices, and `json_schema_extra` metadata. Both the
gateway metadata view and the local config CLI use it, preventing scope,
sensitivity, and editability policy from diverging. `shared/bootstrap.py`
remains the authority for whether a unit's `.env` owns cluster configuration or
a pure runner must fetch it from the gateway.

`redis_bin_dir` is a host-scoped executable selection. The unit's own `.env`
forces or clears `AVA_REDIS_BIN_DIR` at boot through the env registry's
home-authority projection, so a parent's selection cannot leak into a sibling
home. The config view and `_cluster_instance` consume that same Settings field;
the path is not distributed in runner bootstrap. A nonempty directory must
contain both executable Redis tools; an invalid pair fails instead of choosing
a different version from PATH.

Authenticated Linux Redis uses the caller's cluster bearer posture to bind
loopback plus this host's reachable address after the bounded address wait.
macOS keeps its loopback relay workaround; an empty caller bearer stays
loopback-only on either platform. Redis authentication still uses the separate
admin and runtime passwords, not the bearer.
