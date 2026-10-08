# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Anti-Patterns

- **The suppressed batch**:wrapping a batch write in `contextlib.suppress(Exception)` / an empty catch — one bad row rolls back the whole batch with zero trace. → alternative: isolate per-row failures with counters + logs, and let unknown errors crash.
- **The lying counter**:a drop counter that lives inside the suppressed block and never increments — "0 dropped" becomes false assurance. → alternative: increment at the point of failure, outside the suppression; prove it with a regression test.
- **The endless silent retry**:a retry loop that swallows the error and never reports give-up — each retry amplifies load while hiding the cause. → alternative: bounded retries, attempt counters, a logged give-up, and an alert on give-up rate.
- **The theatrical health check**:a liveness flag or PING that never exercises the real path — `is_connected=True` on a dead transport. → alternative: health checks do a real round trip through the actual resource and data path.
- **The un-attributable warning**:a log line with no source id, no context, and no owner — hundreds of copies of a message nobody can locate. → alternative: correlation id + component + bounded inputs, and a rule that unknown-source warnings are themselves filed as debt.
- **Monitoring the process, not the work**:checking "thread alive / process up" while the queue silently drains to zero. → alternative: watch the work product — rows written, events delivered, queue depth — and alarm on absence.

## Sources

- Ava incident records (memory pool `ava/bugs/`):
  - `emitter-silent-batch-rollback-20260804.md` — a suppressed batch rolled back silently; a counter that lied; fix + regression test
  - `watcher-silent-failure-rootcause-2026-08-02.md` — bare except in an infinite silent loop; an expected event's absence unnoticed
  - `sse-queue-full-rootcause-20260804.md` — a full queue dropped 318K events; rate imbalance is only visible as numbers
  - `redis-dead-transport-write-crash-2613.md` — is_connected blind to connection_lost; a fake health check
  - `query-cancellation-source-unknown.md` — 404 unattributable warnings; logs that cannot be owned are noise
  - `metrics-perf-fix-2026-07-17.md` — "which step" beats "how slow overall": per-step timing wins
- Google SRE Book, Chapter 6 "Monitoring Distributed Systems" — the four golden signals (latency, traffic, errors, saturation); monitoring must answer "is it working, and why not".
- addyosmani/agent-skills — production-grade observability skill (ecosystem benchmark).
- Thomas & Hunt, *The Pragmatic Programmer* — the six iron laws of debugging; Tip 62 "don't program by coincidence". See `../../../references/03-pragmatic-programmer.md` §4.2.
- Ousterhout, *A Philosophy of Software Design* — complexity accumulates invisibly (§1.4); the most expensive failures are the ones you cannot see. See `../../../references/01-philosophy-of-software-design.md`.
- **Layer-1 behavioral eval (2026-08-06)** — t3: publish_latency measured the wrong object (handler-internal time, not end-to-end latency), caught by the blind judge(`research/eval/ab/judge-verdict-t3.md`)
