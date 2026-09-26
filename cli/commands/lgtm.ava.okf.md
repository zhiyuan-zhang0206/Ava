---
type: doc
title: Root-owned native LGTM services
description: Preparation, desired service selection and readiness for the observability station.
---

# Root-owned native LGTM services

Loki, Prometheus and Grafana are ordinary `ava-root` units. The canonical ops
roster defines their commands, station eligibility and readiness. macOS uses
the signed helper as root's ancestor; Linux has no helper. There are no per-backend
launchd jobs, systemd units, shell lifecycle commands or foreign-job retirement.

`_lgtm_native.py` prepares pinned, checksum-verified assets and rendered configuration.
`shared/lgtm_local.py` owns executable arguments, local probe addresses and the
configuration digest bound into the root generation. Configuration or roster
changes require a normal stop before a new generation; repeated unchanged start
keeps the existing root. Observation data is retained across stops.

`ava lgtm on` records station intent and enables these three services through
normal start. `ava lgtm off` excludes them from the durable service selection,
preserving other choices and data. A running root refuses a changed generation;
stop it normally before applying the changed selection. The marker or the
`observability-station` capability permits the services, while explicit service
selection can still exclude them.

Readiness requires root-owned listener identity and a successful backend protocol
response. Grafana also reports its database ready. Unknown ownership is unavailable;
a 503 is not ready. Loki write/read diagnostics report ingestion failures separately
and have no authority to launch processes.

Local listen ports are explicit host configuration, independent of external query
URLs. Co-located station homes need distinct ports, including Loki gRPC. Tempo is
remote. `AVA_LGTM_STORAGE_DIR` chooses the retained observation data volume.
Grafana provisioning resolves its database endpoint through the shared direct-DB
mapping and never derives credentials from the cluster bearer.
