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

Runner bootstrap requests advertise `host_unlimited_admission=true` to receive
`AVA_HOST_MAX_CONCURRENT_TURNS=0` as unlimited admission. For older callers without
that capability, the gateway projects only a zero limit to the legacy positive
default of 16; explicit positive limits remain unchanged. This prevents an old
runner from constructing a zero-slot semaphore after a partial cluster update.
New clients can also fetch from an older gateway, which ignores the extra query
parameter. Bootstrap still overwrites stale forwarded or local config values.

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

The local OTLP producer endpoint and collector port are host-scoped. Bootstrap
publishes the read-only `AVA_GATEWAY_OTLP_ENDPOINT` derived from the gateway's
reachable host and fresh OTLP port, overriding any stale copy of that derived
field. Pure-runner collectors and trace replay consume it while retaining
independent local listeners. The relay still requires the cluster bearer.

Remote station ingress resolution lives in `shared/station_endpoint.py` and is
shared by collector rendering and station health probing. Pure stations on the
configured host supply their own ingress URL through `machine_units`; hybrid
units advertise gateway/ops URLs and use the host-scoped
`AVA_OBSERVABILITY_OTLP_PORT` projection (default 4318) instead. Local collector
liveness always uses `telemetry_otlp_port` on loopback, while an explicit
`telemetry_otlp_endpoint` remains a producer export override.

Supervised listener probes use strict socket discovery. Failed inspection yields
`DaemonProbe.unavailable`, which reports the failure and prevents automatic
respawn for that round; it does not certify an absent or healthy listener.
