---
type: doc
title: "OTLP exit flush"
description: "The sole OTLP worker retains SDK resources through finite shutdown; loaded tracing owns its arm and retry exit hooks."
tags:
- base
- telemetry
- otlp
- observability
---

# OTLP exit flush

`base/telemetry/otlp/telemetry_otlp_metrics._build_providers` builds both SDK providers
with `shutdown_on_exit=False`, so neither registers an atexit shutdown of
its own. `base.telemetry.drain_on_exit` is the single ordered exit seam:
the concrete pipeline closes emitter admission and joins its sole writer,
then closes its own OTLP admission and observes that same exporter worker
within the remaining caller deadline. Independent attachments close their own
pipelines through `ClientSet`, without finalizing or shutting another owner. Provider construction,
queue draining, FIFO flush and SDK teardown all stay on that worker. A caller
never becomes a rescue writer or runs provider teardown itself.

## Two wait classes, one deadline

```ava-contract
id = "two-wait-classes-one-deadline"
kind = "invariant"
statement = "A stop takes one real deadline: the writer observation completes or expires before the exporter observation starts, and each native stage receives only the deadline's remaining real time — never the original allowance, and zero once it is spent. A virtualized clock may drive a cancellable cooldown, but never a native stop observation or its budget."
code = [
  "base/telemetry/otlp/telemetry_otlp.py:OtlpBackend.shutdown",
  "base/telemetry/otlp/telemetry_otlp_exporting.py:ExportingEventPipeline.stop",
]
tests = [
  "base/telemetry/tests/otlp/test_otlp_worker_lifecycle.py::test_two_stage_stop_hands_stage_two_the_real_remaining_time",
  "base/telemetry/tests/otlp/test_otlp_worker_lifecycle.py::test_zero_timeout_stage_one_leaves_the_exporter_untouched",
  "base/telemetry/tests/otlp/test_otlp_worker_lifecycle.py::test_blocked_writer_and_blocked_exporter_release_in_stages",
]
```

Waits are two classes. A **cooldown** is the process's own time policy — like
the collector retry gate, it owns no thread or handle and is no completion
condition. A virtualized clock may drive it; a stop cancels and releases it at
once (the deferral's cancel-and-join), and it never consumes the native budget.
A **native observation** waits for a real thread or handle to end — writer
join, deferral clock join, worker join, exporter teardown — and only real
elapsed time ends it, so only real-behavior tests establish its correctness; an
injected clock does not advance while a native wait consumes real time, and a
later stage would read a phantom full allowance. Virtual fast-forward may skip
a cooldown; it can never end a native observation. One stop call takes one
deadline, hands each stage only the real remainder, and leaves an exhausted
remainder at zero.

## Why the seam is single

The SDK default (`shutdown_on_exit=True`) registers a provider shutdown on
atexit at bring-up — mid-life, after `_drain_on_exit`'s import-time
registration — and atexit runs LIFO, so that shutdown fires first. A record
emitted inside the drain thread's final batch window (`_FLUSH_INTERVAL_S` =
0.5 s) then reaches a shut-down processor (`force_flush` returns False) and
stays mirror-only — every short-lived process's tail record (task #4314
triage; fixed by task #4320). Building both providers
`shutdown_on_exit=False` leaves SDK teardown under the OTLP worker reached
through this ordered seam.

## Accepted semantics

- CLI dispatch drains queued telemetry before returning or propagating an
  exception. A probe's only heartbeat can be its first event; deferring provider
  construction until atexit lets the SDK's resource detector encounter an
  already stopped `concurrent.futures` executor (task #5011). The pre-exit drain
  uses the emitter's bounded synchronization and leaves provider shutdown here.
  Commands with no event pipeline do not initialize OTLP.
- Other processes that exit before provider bring-up (task #4314 triage:
  lifetimes under ~0.5 s) never builds providers; its records stay
  mirror-only by design (accepted, task #4320).
- `base/host/exit_codes.py:hard_exit` skips every atexit handler
  including this drain (`os._exit`); the queued batch is deliberately not
  flushed — hard-exit semantics win (accepted, task #4320).
- A deferred hold can begin its existing exporter attempt at shutdown without
  waiting on SDK construction in the caller, subject to the same startup-frozen
  enabled gate as live completion. Disabled holds keep their unexported records
  and report unfinished; they do not construct SDK resources. Empty holds construct nothing;
  interpreter finalization still refuses first construction. The existing age
  timer is canceled and observed finitely. An in-flight completion remains
  owned, and its providers remain available until metric replay has finished.
  Held metrics are replayed once before their logs become visible to the writer.
  See [[export-backpressure.ava.okf.md|OTLP export backpressure]].

Parent node: [[telemetry-otlp.ava.okf.md|OTLP export backend & trace ship to Tempo]].

An unfinished emitter stop reports degraded ordinary delivery and returns without
closing sinks still used by that writer. Queued or held ordinary records can be
lost at process exit; no secondary rescue writer is started. The durable audit
record and SDK participant seal are independent of this observation barrier.

## Lower worker observations

`OtlpWorker` owns one exporter attempt, including a LoggerProvider or metric
reader created before a later SDK construction failure. Named SDK/exporter
exceptions keep their existing best-effort isolation and diagnostics. A failed
SDK teardown is reported as unfinished, even when the Ava worker has ended;
its resources are retained and a later retry opportunity cannot overwrite that
owner. Ordinary failed attempts with completed teardown retain the five-minute
retry gate.

Unknown worker errors are reported immediately and retained as their original
objects. The backend's `shutdown()` raises its original primary error after a
finite join, including errors received after an earlier unfinished return. A
secondary teardown failure cannot replace the primary error. `DrainResult`
acknowledges local processing or observation, not collector durability.

An imported `tracing` module registers its own public `shutdown(timeout=2)`.
It closes arm/retry admission, wakes the retry wait, and finitely observes both
same Threads. A first-use 30-second arm timeout still permits late success
until real shutdown; shutdown does not take the slow SDK initialization lock.
The retry continues to read settings only after each 300-second wait. Cold exit
never imports tracing or starts its SDK merely to shut it down.

These deadlines bound Ava's caller observations. They do not terminate native
SDK work, constrain SDK-owned atexit callbacks, or guarantee a finite whole
process exit. Atexit is not a business caller and does not guarantee a nonzero
exit status for an unknown error. The public owner shutdown methods retain and
raise that error, while its immediate diagnostic remains visible.
