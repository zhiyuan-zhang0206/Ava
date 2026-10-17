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
cluster default for new agent births through `base.agents.model.birth_config`. The
model registry validates writes; existing agents retain their frozen model.

`gateway/app.py` mounts both routers at their existing API paths. Runtime
configuration endpoints obtain their database and pool from the serving
`Request.app.state` and pass them explicitly to the machine lookup, capability
projection and read/audit/write RPC helpers. The audit fanout uses that same
app database. Helpers never import the global gateway app; `ConfigAuthority`
continues to own configuration reads only. Local identity/role reads and fresh
configuration reads retain their existing timing. Router-only HTTP tests cover
two app instances with distinct resources, alongside the full gateway lifespan
integration coverage. Runtime config
tests remain in `gateway/tests/routers/config_api/` and default-model API tests
in `gateway/tests/bootstrap/test_default_model_api.py`.


The ordinary configuration surface remains available when model catalog building
fails. Only resolved-model views and the default-model endpoint acquire that
catalog. Bootstrap also requires the complete provider-key set before returning
any configuration, so it never exports a partial payload.

Machine selection is explicit: a supplied `machine` selects host fields even
when it names the gateway itself. Runner-capable targets use their ops server;
only this gateway without a runner role uses its local owner. A remote host
without that capability never falls back to this host's file. Unknown machine
names are distinct from registered but unavailable hosts.

For combined local host/cluster writes, host validation and application precede
cluster persistence. Revalidate the cluster candidate after the host edit and use
that image's digest for compare-and-swap. A host rejection leaves cluster values
unchanged. Writes persist without an in-memory apply or restart. Audit identity
comes from authenticated middleware state, never the request body.
