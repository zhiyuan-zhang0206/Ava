---
type: doc
title: Clock and timers
description: The injected cluster wall clock and the Timers port for elapsed time and waiting.
tags:
- base
- clock
---

# Clock and timers

`base/clock/` holds the two handles a component reads time through, so that a test
controls time without patching `time` or `asyncio`.

## Wall clock

`Clock` carries the cluster timezone (one zone governs the whole cluster), whether
that name is authoritative for this process, and whether agent-facing timestamps
carry the weekday. A composition root builds it once (`Clock.from_settings()`, or
`Clock(clock_config_from_boot(boot))` for an operation with its own boot view);
components take the `Clock`, never the settings. `now()` is the injectable time
source and `format_timestamp` is the single agent-facing timestamp representation.

## Elapsed time and waiting

`Timers` is a three-method port: `monotonic()`, `sleep(seconds)` and
`await asleep(seconds)`. A component that a test has to drive takes `timers: Timers`
as a required argument, with no default, and calls it in place of `time.monotonic`,
`time.sleep` and `asyncio.sleep`. A component that needs only elapsed time takes
`Timers` and needs no timezone configuration.

- `SYSTEM_TIMERS` is the real implementation. The composition root passes it down.
- `Clock` carries a `Timers` and satisfies `Timers` by delegating to it, so a component
  that needs both the wall clock and elapsed time takes one `Clock` and one argument
  goes all the way down. `Clock(config, timers=...)` requires it; there is no default.
  A root writes `Clock(config, timers=SYSTEM_TIMERS)`; `Clock.from_settings()` is the
  production constructor and uses `SYSTEM_TIMERS` itself. `now=` replaces only the wall
  clock.
- `ManualTimers` (`base/clock/tests/manual_timers.py`) is the test double. It never
  waits: `sleep` and `asleep` move a virtual `monotonic()` forward by the requested
  amount and append the request to `sleeps`, and `advance(seconds)` moves it by hand.
  Its `now()` is a wall clock that moves with that virtual time, so
  `Clock(config, timers=manual, now=manual.now)` gives a component both from one
  source. `asleep` yields to the event loop once, as `asyncio.sleep` does, and a
  negative `sleep` raises like `time.sleep`.

`base/deploy/timing.py` defines another class named `Clock`: the registry that gives
each timing constant its place in the clock lattice. It has nothing to do with this
module.

Parent: [[base/docs/base.ava.okf.md|base library]].
