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
