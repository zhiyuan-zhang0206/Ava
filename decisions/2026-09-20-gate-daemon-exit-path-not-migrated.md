# Gate daemon keeps its synchronous exit — no hard-exit migration

## Context

The daemon-exit sweep (tasks #3940 → #4218 → #4222) moves supervised daemons
off `asyncio.run` ownership of process exit: `Runner.close()` cancel-drains,
then awaits `shutdown_default_executor` behind CPython's 300 s
`THREAD_JOIN_TIMEOUT` cap — equal to the stop flow's entire
`PAUSE_TIMEOUT_SECONDS` budget — and a worker still in flight after that cap
keeps interpreter teardown waiting with no bound at all. The adopted shape is
`asyncio.Runner` (never closed) + an explicit cancellation drain + a local
`_hard_exit` that flushes logs and calls `os._exit` (reference:
`services/agent_ops/daemon.py`, `services/pitr/uploader_daemon.py`).

Sweep-B (task #4224) migrated five daemons — `labeler`, `backup_scheduler`,
`agent_host`, `im_bridge`, `delivery_watchdog` — and checked `gate` (the
fleet's always-up HTTP entry) under the same migrate-by-default stance.

## Decision

`gate` keeps its current synchronous exit path; no hard-exit migration. Its
exit is bounded by construction, so the sweep's shape has nothing to bound
here.

## Alternatives rejected

- **Migrate `gate` anyway, for uniformity.** Rejected — verified on this tree
  (2026-09-20, task #4224): `services/gate/daemon.py` has no `asyncio` use (the
  single occurrence is a comment), and no `to_thread` / `run_in_executor` /
  `concurrent.futures` / `subprocess` / `multiprocessing` / `threading`
  usage. Its listener is a `ThreadingHTTPServer` whose request threads are
  daemon threads (`daemon_threads = True`; `ThreadingMixIn.block_on_close`
  joins only non-daemon threads), and outbound forwarding is synchronous
  `urllib`. On SIGTERM, `install_graceful_shutdown` raises
  `KeyboardInterrupt`, `serve_forever` returns under
  `contextlib.suppress(KeyboardInterrupt)`, and the process exits through
  plain interpreter teardown with no joinable worker threads. Wrapping that in
  `os._exit` would add code that bounds nothing.
- **Leave the disposition open.** Rejected — the sweep's per-daemon table needs
  a recorded verdict so it is not re-litigated; this entry is that record.

## Consequences

- `gate` exits at interpreter-teardown speed, bounded as long as the
  constraints above hold. **If `gate` ever gains an event loop, a thread pool,
  or any non-daemon worker, it must adopt the sweep's `Runner` +
  explicit-drain + `_hard_exit` shape** (reference:
  `services/agent_ops/daemon.py`).
- In-flight request threads die at exit by design — `gate`'s existing accepted
  trade-off for a launchd-stopped always-up entry (documented in its
  `main()`).
