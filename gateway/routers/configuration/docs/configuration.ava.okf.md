---
type: doc
title: Configuration API
description: Runtime configuration editing and the default model for new agent births.
tags: []
---

# Configuration API

`runtime.py` serves `/api/config`. PUT merges a patch into the affected Settings
candidate and validates it before persisting: invalid candidates return 400;
concurrent edits return 409. Host fields target their machine's `.env`, while
cluster and agent fields use the gateway's configuration owner. GET also exposes
the resolved model configuration and its field sources.

`default_model.py` serves `/api/config/default-model`. It reads or updates the
cluster default for new agent births through `base.agents.birth_config`. The
model registry validates writes; existing agents retain their frozen model.

`gateway/app.py` mounts both routers at their existing API paths. Runtime config
tests remain in `gateway/routers/tests/config_api/` and default-model API tests
in `gateway/tests/bootstrap/test_default_model_api.py`.
