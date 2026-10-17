---
type: doc
title: Collector artifact download
description: The pinned otelcol-contrib fetch — dependency closure, wall-clock cap and its evidence.
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
(`base/host/net/ports.py`) and `timers` (`base/clock.py`). The composition root
passes `SYSTEM_URL_OPENER` and `SYSTEM_TIMERS`. `download_with_retry` waits its
backoff through the caller's clock — `retry(_DOWNLOAD_POLICY, sleep=timers.sleep)`
— so a caller that passes `ManualTimers` records the waits and advances virtual
time instead of blocking, and the reported per-attempt and total elapsed times
come from that same clock. The retry loop itself stays in its owner,
`base/host/net/resilience.py`.

## Wall-clock cap

One attempt is bounded by `attempt_timeout_s` (default
`_DOWNLOAD_ATTEMPT_TIMEOUT_S`, 600 s). The budget is checked before and after
every socket read and the body is read with `read1` (at most one socket read per
call), so a mirror that keeps trickling data cannot stretch an attempt; the
empty read that ends the body is checked too, so a connection that closes late
cannot turn an over-budget attempt into a reported success. The one wait no
check can interrupt is a read already blocked on the socket, bounded by
`DOWNLOAD_SOCKET_TIMEOUT_S` (30 s, the per-read guard set on the request): an
attempt therefore ends by `attempt_timeout_s + DOWNLOAD_SOCKET_TIMEOUT_S` at the
latest. A progress heartbeat line every 15 s distinguishes a slow mirror from a
dead one, and the response and the destination file close on every exit path.

The regression tests scale this budget down to a second and keep the bound's
shape: `test_collector_artifact_retry.py` drives a real local drip server
(trickle, then a late close) and the stream cases in
`cli/commands/observability/tests/converge_otel_collector/` pin the byte stream
and the per-read timeout reaching the opener.

The collector download bounds one attempt by an absolute wall-clock cap checked before and after every socket read, including the empty read that ends the body; reads use read1 (one socket read per call) and the residual overshoot is one in-flight read, bounded by DOWNLOAD_SOCKET_TIMEOUT_S.

The collector download waits its retry backoff through the caller's timers (retry(policy, sleep=timers.sleep)); reported elapsed times use the same clock, a ManualTimers records the waits instead of blocking, and the retry loop stays in base/host/net/resilience.py.

Parent: [[base/deploy/docs/deploy.meiva.okf.md|Deployment primitives]].
