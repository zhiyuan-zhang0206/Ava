---
type: doc
title: Collector artifact download
description: The pinned otelcol-contrib fetch — dependency closure, the one download deadline and its evidence.
tags: [base, deploy]
---

# Collector artifact download

`base/deploy/release/collector_artifact.py` acquires the pinned otelcol-contrib
tarball on the `meiva start` critical path, verifies its SHA256 and installs the
binary beside its version marker. The converge step
(`cli/commands/observability/otel_collector.py:ensure_otel_collector`) is its
composition root.

## Dependency closure

The operation takes its own dependencies as required arguments: `open_url`
(`base/host/net/ports.py`), `timers` (`base/clock.py`) and its numbers
(`DownloadLimits`). The composition root passes `SYSTEM_URL_OPENER`,
`SYSTEM_TIMERS` and the `otel_collector_download_*` settings. `download_with_retry`
waits its backoff through the caller's clock — the `sleep` hook it hands to
`retry()` calls `timers.sleep` — so a caller that passes `ManualTimers` records
the waits and advances virtual time instead of blocking, and the reported
per-attempt and total elapsed times come from that same clock. The retry loop
itself stays in its owner, `base/host/net/resilience.py`.

## One deadline per download

A download operation has exactly ONE deadline. `download_with_retry` computes
`deadline = started + limits.total_budget_s` once and hands it to every attempt;
`stream_download` spends only `remaining = deadline - now`. There is no
per-attempt cap and no second clock: the retry wait is this operation's own
`timers.sleep`, clamped to the budget that remains, so a backoff can never run
past the deadline, and the reported elapsed times are the same budget minus the
same remaining.

Every read is preceded by a remaining-budget check, so a mirror that keeps
trickling data cannot stretch an operation: it ends at the deadline. The empty
read that ends the body is checked too, so a connection that closes late cannot
turn an over-budget attempt into a reported success. The request opens with
`socket_timeout` = `min(limits.socket_timeout_s, remaining)`, which bounds the
one wait no check can interrupt — a read already blocked on the socket. The
residual overshoot is therefore one in-flight read, at most
`min(socket_timeout_s, total_budget_s)`.

Every failure reports used time, remaining budget and bytes received, at the
attempt line and in the final `RuntimeError`. A progress heartbeat every
`limits.progress_interval_s` distinguishes a slow mirror from a dead one, and
the response and the destination file close on every exit path.

The numbers are the `otel_collector_download_*` settings (observability domain,
`MEIVA_OTELCOL_DOWNLOAD_*`), built into `collector_artifact.DownloadLimits` by
the converge composition root (`ensure_otel_collector`); the module itself
reads no settings.

The regression tests scale every number down and keep the bound's shape:
`test_collector_artifact_retry.py` drives real local servers (a drip with and
without `Content-Length`, a connection that never sends headers, a late close)
and pins the wait accounting with `ManualTimers`.

```meiva-contract
id = "bounded-download-attempt"
kind = "invariant"
statement = "One collector download operation holds a single absolute deadline (started + total_budget) that every attempt and every retry wait is charged to; each socket read is preceded by a remaining-budget check, the empty read that ends the body is checked too, each request opens with min(socket_timeout_s, remaining), and a backoff is clamped to the remaining budget, so the residual overshoot is one in-flight read bounded by that request's socket timeout (at most min(socket_timeout_s, total_budget_s))."
code = ["base/deploy/release/collector_artifact.py:stream_download", "base/deploy/release/collector_artifact.py:download_with_retry", "base/deploy/release/collector_artifact.py:DownloadLimits", "base/deploy/release/tests/test_collector_artifact_retry.py:<module>"]
tests = ["base/deploy/release/tests/test_collector_artifact_retry.py::test_a_dripping_mirror_cannot_outlive_the_operation_deadline", "base/deploy/release/tests/test_collector_artifact_retry.py::test_a_connection_that_never_sends_headers_ends_within_the_socket_guard", "base/deploy/release/tests/test_collector_artifact_retry.py::test_a_body_that_closes_after_the_deadline_is_not_a_success", "base/deploy/release/tests/test_collector_artifact_retry.py::test_a_wait_never_runs_past_the_deadline", "cli/commands/observability/tests/converge_otel_collector/test_session_filelog_receivers_are_disjoint_and_bound.py::test_stream_download_writes_all_chunks", "cli/commands/observability/tests/converge_otel_collector/test_session_filelog_receivers_are_disjoint_and_bound.py::test_stream_download_honors_socket_timeout"]
```

```meiva-contract
id = "download-wait-closure"
kind = "invariant"
statement = "The collector download waits its retry backoff through the caller's timers, clamped to the budget the attempts left, so a wait never runs past the operation's deadline; reported elapsed and remaining times use the same clock, a ManualTimers records the waits instead of blocking, and the retry loop stays in base/host/net/resilience.py."
code = ["base/deploy/release/collector_artifact.py:download_with_retry", "base/deploy/release/tests/test_collector_artifact_retry.py:test_download_with_retry_preserves_contract"]
tests = ["base/deploy/release/tests/test_collector_artifact_retry.py::test_download_with_retry_preserves_contract", "base/deploy/release/tests/test_collector_artifact_retry.py::test_a_retry_wait_is_spent_from_the_same_budget", "base/host/net/tests/test_resilience.py::TestWaitHooks::test_retry_uses_the_callers_own_wait"]
```

Parent: [[base/deploy/docs/deploy.meiva.okf.md|Deployment primitives]].
