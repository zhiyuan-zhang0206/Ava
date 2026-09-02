# Watchdog tick in-flight guard

## Decision

After a watchdog tick exceeds its deadline, retain its asyncio task as the
capability's sole in-flight round. Later cadences skip their whole round until
that task returns instead of starting another controller or healthcheck.

## Why

The deadline can cancel only the awaiter. It cannot stop the synchronous worker
thread used by controllers and healthchecks, so starting a new tick immediately
would run duplicate, concurrent work against the same unit. Keeping the
deadline-detached task shielded preserves the timeout's stale health signal
without pretending its underlying work ended.

## Consequences

- The watchdog remains responsive enough to serve stale health while an
  unkillable worker finishes.
- A wedged round holds later rounds back rather than producing concurrent
  reconciles or repeated service respawns.
- An unbounded controller's own `TimeoutError` is not formatted as a manager
  deadline; it propagates with its original context.
