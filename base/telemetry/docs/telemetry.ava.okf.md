---
type: doc
title: Telemetry primitives
description: Metrics, OTLP export, resource labels and durable event ingestion.
tags: [base]
---

# Telemetry primitives

`base/telemetry/` owns metrics, OTLP export, resource labels and durable event ingestion.
Its component nodes describe the current contracts and implementation.

## Documented components

- [[base/telemetry/metrics/docs/metrics.ava.okf.md]] — Metrics.
- [[base/telemetry/otlp/docs/telemetry-otlp/telemetry-otlp.ava.okf.md]] — OTLP export backend & trace ship to Tempo.

## Ordinary event delivery lifecycle

`EventPipeline` owns the bounded queue, admission, one drain worker and its
terminal result. Only that worker writes batches. `flush()` and `sync()` use
independent FIFO receipts; the worker acknowledges each receipt after writing
its preceding queued and held records. Every barrier has one finite deadline,
including queue admission. Concurrent barriers cannot consume each other's ack.
A completed receipt acknowledges worker processing; explicitly isolated sink
failures remain independently reported rather than becoming a persistence ack.

`stop(timeout=5)` closes admission before requesting exit and joining the same
worker within its deadline. Its stop request is outside the bounded event queue,
so saturation cannot make stop block on a sentinel. A stuck writer leaves an
explicit `DrainResult(status=DrainStatus.UNFINISHED, phase=...)` and a degraded diagnostic;
ordinary records may be lost at process exit or land after the caller returns.
No rescue writer is started. A later stop can observe late completion. The exit
hook closes downstream sinks only after the event worker has finished.

An unknown worker failure is reported immediately and retained with its original
exception. Active `sync()`/`flush()` and the owner's `stop()` re-raise it rather
than acknowledging delivery. Later ordinary producers keep the existing shedding
contract. Explicit best-effort sink/exporter isolation remains in its existing
seams and does not become a business failure.

These results describe the ordinary observation projection. Durable `audit_events`
are committed in the producer's transaction before projection admission; SDK
capture journals and participant sealing retain their independent durability
contracts. An unfinished observation barrier is never a commit or seal receipt.

The public `EventPipeline` resource lives in `delivery/pipeline.py`; construction starts
its worker and requires the root's writer. `build_pipeline(database=...)` binds
ordinary sinks to that root's database factory. Controlled host/exec logging binds
this actual writer through `init_telemetry(pipeline=..., machine_reader=...)`;
legacy process calls remain supported until their own roots migrate. SDK ClientSet roots retain this
constructor without invoking it until a selected event enqueues. Quiet sync and
close never build it. Finite close retains an unfinished or failed writer for
later observation, and an original worker failure propagates after other client
cleanup. The exec child observes its owned writer before finalizing OTLP and
forming a successful post-delivery outcome; attachments drain before detach.

The host joins native resources before closing its constructed ClientSet writer
and releasing settled ownership. A writer failure still permits bounded lease
release, pool cleanup and pidfile removal. The first original native, boot or
writer failure remains primary; later cleanup failures are attached as notes.
Unfinished native resources retain the clients and pools until process exit.
