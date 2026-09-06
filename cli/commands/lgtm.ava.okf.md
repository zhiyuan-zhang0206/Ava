---
type: doc
title: Native LGTM lifecycle
description: Verified native assets, home-scoped service ownership, local listeners, and lifecycle probes for the observability station.
tags:
- cli
- observability
- lifecycle
---

# Native LGTM lifecycle

`_lgtm.py` gates converge on the home marker or `observability-station`
capability. `_lgtm_native.py` downloads and verifies the pinned archives,
renders native configs, and registers launchd jobs on Darwin arm64 or user
systemd units on Linux amd64. `_lgtm_assets.py` holds the common command specs
and selects platform-specific URLs, checksums, and archive members from
`deploy/lgtm/native/versions.yml`. Version and platform markers prevent reuse
of a copied executable from another architecture.

`shared/lgtm_systemd.py` owns only the three units whose names include this
home's slug. Loaded `FragmentPath` must match the canonical user-unit file;
a running service also requires `/proc/<MainPID>/exe` to match this home's
installed binary. An unrelated HTTP listener cannot satisfy that proof.
Units use `Restart=on-failure` and a bounded control-group stop. Failed user
manager commands and failed Loki config validation propagate to the caller.
No foreign units, system services, or container deployments are modified.

`shared/lgtm_local.py` derives local probe URLs from native host/port settings,
independently of remote telemetry query URLs. Linux lifecycle and the watchdog
bypass machine HTTP proxies when probing these listeners. The Darwin launcher
receives the same resolved URLs from CLI and watchdog callers. Native ports
are host-scoped configuration; the regular cluster port block does not assign
them. Separate homes therefore need explicit non-overlapping ports, including
Loki's gRPC listener.

Converge rewrites configs on every invocation. On Linux, changed unit/config
inputs restart only already running owned services; the following start step
starts stopped services and waits for owned HTTP listeners. A repeated start
leaves working services alone. `ava lgtm off` removes the marker before
stopping and removing all three home-scoped service definitions, including
Grafana. Data directories remain intact. Tempo remains a remote service and
is outside this lifecycle.

Operator prerequisites, defaults, and storage paths are documented in
[the deployment guide](../../deploy/lgtm/README.md). User lingering and WSL
startup policy are explicit host preparation; native LGTM does not configure
them as a side effect.
