---
type: doc
title: OTLP Export Backpressure
description: The bounded queue, network deadlines, circuit breaker, and flush ceilings that keep collector failure outside agent turns.
tags:
- shared
- telemetry
- otlp
---

# OTLP Export Backpressure

## Contract

OTLP failure cannot block or break the event drain after its JSONL mirror write,
and a collector outage cannot add an unbounded wait to an agent turn. Loss is an
explicit bounded-degradation policy: producers count and report shedding while
the collector sidecar owns durable mirroring after acceptance.

## Signal paths

- `shared/telemetry_otlp.py` places events into a bounded 2048-entry queue with
  `put_nowait`; a full queue increments the cumulative drop counter and reports
  the loss outside the saturated log lane. In-memory metric recording never waits on the
  exporter queue.
- SDK-owned log and metric threads perform network I/O. Both OTLP/HTTP
  exporters and their processors receive explicit 2-second deadlines; errors
  cannot raise into an Ava producer thread.
- `shared/trace.py:OtlpJsonHttpSpanExporter` gives each protobuf trace batch one
  2-second POST. Three consecutive failed batches open a 30-second circuit.
  During cooldown it returns `FAILURE` without encoding or posting and counts
  dropped batches and spans; after cooldown exactly one half-open probe runs.
  Probe success closes the circuit and resets consecutive failures, while probe
  failure starts a fresh cooldown.
- Exec children drain their application queue without blocking, then request a
  500 ms force-flush budget from each SDK provider. The HTTP exporters retain
  their own 2-second network ceiling even when an SDK implementation does not
  propagate that shorter budget. Shutdown requests a separate 2-second budget
  per provider and never runs inside the turn body.

## Recovery

The initial collector reachability probe is bounded at 1.5 seconds. A missing
sidecar disables exporter construction for five minutes, reports the episode,
and retries on the backend's next initialization opportunity; trace setup uses
one daemon retry loop on the same cadence. Successful trace recovery reports the
preceding consecutive failures and cumulative circuit drops.

Parent node: [[telemetry-otlp.ava.okf.md|OTLP export backend and trace ship]].

## Queue loss

The emitter, OTLP log queue, and OTel SDK batch queue report actual loss as `event_log_drop`, with
`n`, `queue`, and `last_dropped_at`. Local error diagnostics bypass the saturated
queue and always write stderr, including standalone scripts before logging setup.
Delayed summaries preserve the actual loss time. OTLP loss reports go straight to the JSONL mirror and metric instruments;
`ava_event_log_drop_last_dropped_at_ratio` drives the immediate error-level
`ava-ops-telemetry-queue-loss` Grafana alert, resolving after five loss-free minutes.
The metrics lane can report even while log delivery is saturated. A collector or
metrics-export outage still delays remote visibility; existing exporter-silence
alerts cover that failure. These are observable losses, not a durable delivery
guarantee or a reconstruction of missing calls.

The OTel SDK observer handles its pinned queue-full warning before OTel's
duplicate-log filter, records each loss, and suppresses the original warning
after replacing it with an error outside the emitter. A real bounded SDK queue
test locks this upstream diagnostic contract.
