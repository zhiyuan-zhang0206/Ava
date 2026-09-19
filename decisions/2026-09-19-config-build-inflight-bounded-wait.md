# Bounded cross-thread wait for the in-flight config build window

## Context

The boot-lite chain (decisions/2026-09-16-config-boot-lite.md) constructs the
eager config chain on the first read the lite table cannot serve: single-shot,
serialized by one RLock, under which the build runs. While that build was in
flight, every other reading thread received the documented "in flight"
AttributeError instead of a value -- the window was accepted as a failure
surface because it is normally sub-second.

On 2026-09-19 the window surfaced as a user-visible hard failure (task #4069):
in a long-running agent process, two concurrent `ava.web.fetch` workers raced
the process's first web-domain read; the loser raised "config boot-lite:
settings.web.web_jina_reader_base is not readable while the eager config build
is in flight" and the caller fell back to fetching by hand. Any concurrent
first touch can land in the window, and a read that could simply wait should
not fail.

## Decision

A read that arrives while another thread's build is in flight waits for it,
bounded. Only the building thread itself keeps the serve-lite/raise behavior --
it must never wait on its own build:

- `_maybe_upgrade` compares the reader's thread identity with the builder's
  (`_state.upgrading_thread`, set inside `upgrade()` under the lock). Same
  thread: previous behavior (serve the lite value, or raise the window error).
  Other threads: wait.
- The wait acquires the upgrade lock with a timeout of
  `_BUILD_WAIT_TIMEOUT_SECONDS` (30s). The bound is a marked
  internal-invariant constant, not config (`conventions/numeric-limits.md`,
  task #3696 exception inventory):
  the wait runs before the config chain exists -- reading a field would itself
  trigger the upgrade -- and the value answers to an internal invariant, not a
  tuning surface: 3x the bootstrap fetch bound (`shared/bootstrap.py`
  `_FETCH_TIMEOUT_S = 10s`), the slowest legitimate segment of a build. A
  slower build degrades to the retryable `ConfigBuildWaitTimeoutError`, so no
  operator knob is warranted. The builder holds the lock across the whole
  upgrade, so the acquisition is the wait; once acquired, the chain is
  installed and the read serves the full value.
- On expiry the read raises `ConfigBuildWaitTimeoutError` -- an AttributeError
  subclass whose message says the build is still running and the read may be
  retried. The wait itself corrupts nothing.
- If the in-flight attempt died before installing (its build raised), the
  waiting thread runs the build itself, so the real construction error
  surfaces to that reader instead of a misleading window error.
- `get_field()` takes the same bounded wait on its upgrade path. The view
  `__setattr__` paths are unchanged: a write during the window keeps its
  existing lock-ordered wait.

## Alternatives rejected

- Keep the hard error and document "retry later": rejected -- the incident
  shows callers treat it as a hard failure (the caller fell back to a manual
  fetch), and a short wait costs nothing when the build succeeds.
- Unbounded wait: rejected -- a stalled build would hang every reader with no
  signal; the bound converts that into a clearly retryable error.
- Event / condition variable for build completion: rejected -- the upgrade
  lock already serializes the build and carries the wait; one mechanism, not
  two.
- Serve the lite value or a placeholder on window reads: rejected -- non-lite
  fields have no lite value, and placeholder reads would mask construction
  failures (fail-fast discipline).

## Consequences

- The window is no longer a failure mode for concurrent readers; their latency
  includes the residual build time, capped by the wait bound.
- A stuck build surfaces as a retryable `ConfigBuildWaitTimeoutError` instead
  of a hang or a mislabeled "in flight" error.
- Pinned by `tests/shared/test_config_boot_lite.py`: the concurrent first-touch
  battery (8 threads, one upgrade, zero errors), a deterministic two-thread
  wait on a gated build, the timeout/retryable-error path, the same-thread
  no-self-wait invariant, and the facade export identity.
