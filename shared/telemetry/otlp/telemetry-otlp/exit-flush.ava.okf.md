---
type: doc
title: "OTLP exit flush"
description: "Both SDK providers build with `shutdown_on_exit=False`; `shared.telemetry._drain_on_exit` is the single ordered exit seam, and the short-process / hard-exit semantics stay accepted."
tags:
- shared
- telemetry
- otlp
- observability
---

# OTLP exit flush

`shared/telemetry/otlp/telemetry_otlp_metrics._build_providers` builds both SDK providers
with `shutdown_on_exit=False`, so neither registers an atexit shutdown of
its own. `shared.telemetry._drain_on_exit` is the single ordered exit seam:
it flushes the emitter, then `telemetry_otlp.shutdown()` completes any
active deferred hold, drains the OTLP queue, and force-flushes the
still-live providers.

## Why the seam is single

The SDK default (`shutdown_on_exit=True`) registers a provider shutdown on
atexit at bring-up — mid-life, after `_drain_on_exit`'s import-time
registration — and atexit runs LIFO, so that shutdown fires first. A record
emitted inside the drain thread's final batch window (`_FLUSH_INTERVAL_S` =
0.5 s) then reaches a shut-down processor (`force_flush` returns False) and
stays mirror-only — every short-lived process's tail record (task #4314
triage; fixed by task #4320). Building both providers
`shutdown_on_exit=False` leaves the drain's flush as the only exit-path
shutdown call.

## Accepted semantics

- A process that exits before provider bring-up (task #4314 triage:
  lifetimes under ~0.5 s) never builds providers; its records stay
  mirror-only by design (accepted, task #4320).
- `services/agent_ops/daemon.py:_hard_exit` skips every atexit handler
  including this drain (`os._exit`); the queued batch is deliberately not
  flushed — hard-exit semantics win (accepted, task #4320).
- Exit-time completion of a deferred hold is `shutdown()`'s `finalize()`
  call (task #3816 M4b); deferral semantics live in
  [[export-backpressure.ava.okf.md|OTLP export backpressure]].

Parent node: [[telemetry-otlp.ava.okf.md|OTLP export backend & trace ship to Tempo]].
