---
type: doc
title: Root-owned native LGTM services
description: Preparation, desired service selection and readiness for the observability station.
---

# Root-owned native LGTM services

Loki, Prometheus and Grafana are ordinary `meiva-root` units. The canonical ops
roster defines their commands, station eligibility and readiness. macOS uses
the signed helper as root's ancestor; Linux has no helper. There are no per-backend
launchd jobs, systemd units, shell lifecycle commands or foreign-job retirement.

`lgtm_native.py` prepares pinned, checksum-verified assets and rendered configuration.
`base/telemetry/lgtm_local.py` owns executable arguments, local probe addresses and the
configuration digest bound into the root generation. Configuration or roster
changes require a normal stop before a new generation; repeated unchanged start
keeps the existing root. Observation data is retained across stops.

`meiva lgtm on` records station intent and enables these three services through
normal start. `meiva lgtm off` excludes them from the durable service selection,
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
remote. `MEIVA_LGTM_STORAGE_DIR` chooses the retained observation data volume.
Grafana provisioning resolves its database endpoint through the shared direct-DB
mapping and never derives credentials from the cluster bearer.

## One deadline per asset download

Each pinned archive (Loki ~46 MB, Prometheus ~105 MB, Grafana ~461 MB) is one
download operation with exactly ONE deadline: `download_with_retry` computes
`deadline = started + budget` once and hands it to every attempt, and
`stream_download` spends only `remaining = deadline - now`. There is no
per-attempt cap, and the three assets do not share a step budget — a slow Grafana
mirror cannot eat Loki's or Prometheus's time.

Every read is preceded by a remaining-budget check, and the empty read that ends
the body is checked too, so a trickling mirror stops at the deadline instead of
stretching the operation. The request opens with `min(socket_timeout, remaining)`;
that bounds the one wait no check can interrupt, so the residual overshoot is one
in-flight read, at most `min(socket_timeout_s, total_budget_s)`.

The retry wait is this operation's own `timers.sleep`, clamped to the budget that
remains, so a backoff never runs past the deadline. Every failure reports used
time, remaining budget and bytes received, in the attempt line and in the final
`RuntimeError`.

The numbers are the `lgtm_native_download_*` settings (observability domain,
`MEIVA_LGTM_NATIVE_DOWNLOAD_*`) built into `lgtm_native.DownloadLimits` by
`ensure_lgtm_native`; the download functions take them as data. The 900 s default
is measured: on a 5.8-8.7 MB/s link the three archives took 8 s / 14 s / 53 s
(2026-10-17), so 900 s covers the 461 MB Grafana tarball at >=512 KB/s.

```meiva-contract
id = "lgtm-native-download-deadline"
kind = "invariant"
statement = "One native LGTM archive download holds a single absolute deadline (started + budget) charged by every attempt and every retry wait; each socket read is preceded by a remaining-budget check, the empty read that ends the body is checked too, each request opens with min(socket_timeout_s, remaining), and a backoff is clamped to the remaining budget, so the residual overshoot is one in-flight read bounded by that request's socket timeout (at most min(socket_timeout_s, total_budget_s))."
code = ["cli/commands/observability/lgtm_native.py:stream_download", "cli/commands/observability/lgtm_native.py:download_with_retry", "cli/commands/observability/lgtm_native.py:DownloadLimits", "cli/commands/observability/tests/test_lgtm_download.py:<module>"]
tests = ["cli/commands/observability/tests/test_lgtm_download.py::test_a_dripping_mirror_cannot_outlive_the_asset_deadline", "cli/commands/observability/tests/test_lgtm_download.py::test_a_connection_that_never_sends_headers_ends_within_the_socket_guard", "cli/commands/observability/tests/test_lgtm_download.py::test_a_wait_never_runs_past_the_deadline", "cli/commands/observability/tests/test_lgtm_download.py::test_a_retry_wait_is_spent_from_the_same_budget"]
```

Operator selection, label verification and replay are in [observability operations](../operations.md); local rotation and retention are in [log maintenance](../log-maintenance.md).

A dashboard can disappear while every readiness probe still passes: the render fallback
that keeps `meiva-ops` present, the sidecar it reconciles, and the four-layer check that
refuses the absence are in [dashboard provisioning](dashboard-provisioning.meiva.okf.md).
