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
`Clock(clock_config_from_boot(boot), timers=SYSTEM_TIMERS)` for an operation with its own boot view);
components take the `Clock`, never the settings. `now()` is the injectable time
source and `format_timestamp` is the single agent-facing timestamp representation.

`base.clock` has no configuration import while loading its capabilities. The
timezone formatting value, real timers and an explicitly constructed Clock are
usable before configuration admission. `Clock.from_settings()` reads live settings
only when called. A boot owner imports `clock_config_from_boot` from
`base.clock.configuration`; this assembly module is allowed to load configuration.
There is no forwarding export from the capability module. Importing a capability
must not create `AVA_HOME` or trigger configuration-dependent startup work.

The boot factory snapshots the supplied view. A previously built Clock retains its
timezone and weekday policy after that owner changes, and a different ConfigBoot
is independent. `authoritative_timezone` is absent when the timezone field was not
explicitly set. `zone()` treats absent or unparseable authority as the host-zone
signal, while `explicit_zone()` uses the configured timezone even when it was only
the default. Callers choose the intended interpretation; no fallback timezone is
silently substituted by the explicit path.

Timestamp formatting converts timezone-aware database or injected values into
the cluster zone. The standing context note supplies that zone once, so each
timestamp omits a zone suffix: `%Z` would repeat a cluster fact and can be
ambiguous across DST or regions. `base.config.apply_cluster_timezone()` and
`host_tz_name()` remain configuration/process concerns; they change or inspect
the process and host rather than a Clock handle.

```ava-contract
id = "configuration-free-capabilities"
kind = "invariant"
statement = "Importing and using Clock, ClockConfig, Timers and SYSTEM_TIMERS does not load configuration; invoking the settings factory is the boundary that requests it."
code = ["base/clock/__init__.py:<module>", "base/clock/tests/test_cold_import.py:test_clock_capabilities_stay_cold_until_the_settings_factory_runs"]
tests = ["base/clock/tests/test_cold_import.py::test_clock_capabilities_stay_cold_until_the_settings_factory_runs", "base/clock/tests/test_clock.py::test_from_settings_reads_the_live_settings"]
```

The cold test launches a fresh isolated interpreter with an uncreated home. Its
stdlib-only child installs an import blocker before importing any Clock symbol,
then exercises wall formatting and synchronous/asynchronous timer delegation. The
same blocker must reject the settings factory. The parent checks the complete
child exit and confirms that the home is still absent; it does not warm imports or
erase loaded modules to simulate cold startup.
The subprocess uses the running test interpreter and a repository-owned child
path with no shell or caller-supplied executable. A subprocess is essential here:
the pytest worker has already imported application configuration.

```ava-contract
id = "boot-clock-snapshot"
kind = "invariant"
statement = "A boot ClockConfig captures only its supplied owner's timezone authority and timestamp policy, independently of older Clock handles and other owners."
code = ["base/clock/configuration.py:<module>"]
tests = ["base/clock/tests/test_clock.py::test_boot_clock_reads_keep_two_owners_and_previous_handles_separate", "base/clock/tests/test_clock.py::test_the_explicit_zone_follows_the_field_even_when_it_is_the_default", "base/clock/tests/test_clock.py::test_an_unparseable_authoritative_name_degrades_to_the_host_zone_signal"]
```

## Elapsed time and waiting

`Timers` is a three-method port: `monotonic()`, `sleep(seconds)` and
`await asleep(seconds)`. A component that a test has to drive takes `timers: Timers`
as a required argument, with no default, and calls it in place of `time.monotonic`,
`time.sleep` and `asyncio.sleep`. A component that needs only elapsed time takes
`Timers` and needs no timezone configuration.

Only differences between monotonic readings carry meaning. Synchronous sleep
blocks the caller's thread; asynchronous sleep suspends its task while allowing
other event-loop tasks to run. Supplying a Timers does not turn unrelated native
blocking operations into virtual-time operations.

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

The single Clock implementation is retained: injection does not create a second
timezone policy or require parallel wall-clock and timer arguments. Replacing
`now` changes wall time only; native monotonic time remains owned by the supplied
Timers. ManualTimers is a sequential test double, not a concurrent scheduler: its
waits immediately advance shared virtual time. It does not establish correctness
for real blocking I/O or cross-task deadline races.

`base/deploy/timing.py` defines another class named `Clock`: the registry that gives
each timing constant its place in the clock lattice. It has nothing to do with this
module.

Parent: [[base/docs/base.ava.okf.md|base library]].
