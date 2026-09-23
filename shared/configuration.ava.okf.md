---
type: doc
title: Configuration and Bootstrap
description: 'Runtime Settings, field metadata, local env ownership, and bootstrap distribution.'
tags:
- configuration
- infrastructure
---

# Configuration and Bootstrap

`shared/config/` defines the per-domain settings models and the public
`settings` facade. Runtime import boots **lite** (`shared/config/_lite.py`):
it loads `.env` plus the env-authority pass, applies the cluster clock, and
fail-fast validates the boot-path field index, without importing the 15
sub-models, `pydantic_settings`, or building the registry. The eager chain
(`shared/config/_full.py`) is constructed once on the first access the lite
layer does not cover -- an upgrade that replays pending overlay writes and
rebinds the facade's `settings` name; pre-upgrade bindings keep working.
A read that lands while that build is in flight on another thread waits for
it instead of failing (bounded; expiry raises the retryable
`ConfigBuildWaitTimeoutError`), and the building thread's own re-entrant reads
keep the lite value. The
gateway, ops daemons and agent host call `shared.config.ensure_eager()` to
keep full fail-fast at boot; `AVA_CONFIG_BOOT=eager` (process env only) is the
operator escape hatch, and existing settings-lite `AVA_CONFIG_FETCH=skip`
verbs keep deferring everything until first attribute access (`skip` wins over
`eager`), so metadata-only repair code can load model declarations without
reading a broken local `.env` or fetching runner configuration.

The boot-path index is generated: `shared/config_lite_table.json` (read by the
hand-written `shared/config_lite_table.py`, outside this package because
`shared/env_registry.py` consumes its surfaces before Settings exists) is
produced from the live registry by `scripts/gen_config_lite_table.py` and
byte-compared by the `config-lite-table-fresh` gate. A field a process never
touches is no longer validated at import (the accepted semantic change of task
#3621): local-source units keep their import-time required-field check, a
configured runner still fetches bootstrap at the same point, and the
equivalence windows plus first-error parity with the eager path are pinned by
tests (`tests/shared/test_config_lite_*.py`); the design record is
[2026-09-16-config-boot-lite](../decisions/2026-09-16-config-boot-lite.md).

`shared/config_registry.py` is the single projection of field aliases,
annotations, editor types, choices, and `json_schema_extra` metadata. Both the
gateway metadata view and the local config CLI use it, preventing scope,
sensitivity, and editability policy from diverging. `shared/bootstrap.py`
remains the authority for whether a unit's `.env` owns cluster configuration or
a pure runner must fetch it from the gateway.

Bootstrap serves `AVA_HOST_MAX_CONCURRENT_TURNS` verbatim, including zero for
unlimited admission. Runner requests select only the credential role. Snapshot
version 2 carries these values; mismatched versions trigger a fresh fetch.
Same-version snapshots retain the 300s freshness and transport-outage behavior.
Bootstrap overwrites stale forwarded or local config values when a snapshot or
fetch applies.

`redis_bin_dir` is a host-scoped executable selection. The unit's own `.env`
forces or clears `AVA_REDIS_BIN_DIR` at boot through the env registry's
home-authority projection, so a parent's selection cannot leak into a sibling
home. The config view and `_cluster_instance` consume that same Settings field;
the path is not distributed in runner bootstrap. A nonempty directory must
contain both executable Redis tools; an invalid pair fails instead of choosing
a different version from PATH.

`pg_throwaway_base` is the host-scoped scratch-cluster selection: where
`shared/pg_tools.throwaway_postgres` creates disposable Postgres instance dirs
(test fixtures, smokes, the restore drill). Empty keeps the platform default —
`/dev/shm` on Linux, the OS temp dir elsewhere — and `shared/pg_throwaway_base.py`
demotes a caller that declares its required capacity to the disk fallback
(`/var/tmp` where present) when the tmpfs cannot hold it. It rides the same
home-authority projection as `AVA_REDIS_BIN_DIR`, so a parent's selection cannot
leak into a sibling home.

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
