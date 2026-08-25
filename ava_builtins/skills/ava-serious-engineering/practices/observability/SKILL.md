---
name: observability
description: "Makes production failures visible through correlated logs, metrics, critical-path events, and watchdogs. Use when designing or reviewing queues, batch writers, background jobs, remote services, watchers, or any system that could fail silently."
---

# Observability

## One-Sentence Core
> A failure you can see is an incident; a failure you cannot see is a mystery. Observability is the discipline of making every failure class — especially the silent ones — visible, attributable, and locatable in minutes, not days.

## Core Principles

- **Silent failure is the worst failure**:A system that fails and says nothing has failed twice: once in the work, once in the trust. Every failure class — dropped item, rolled-back batch, dead connection, stuck retry — must carry a visible, countable, alerted signal. — **Why**:Real incident (Ava telemetry, 2026-08): a batch writer wrapped in `contextlib.suppress(Exception)` hit one foreign-key violation in a batch of rows; the entire batch rolled back silently, the `dropped` counter stayed at 0, no log was written. Operators saw "emitted, no rows in DB, queue empty, thread alive" — every surface looked healthy while data vanished. A second incident: a CI watcher looped forever inside `except Exception: pass`, never reporting — silent retries are failure amplifiers, each iteration adding load while hiding the original error. — **How**:Never suppress an exception without, at the point of failure: incrementing a counter and writing a structured log line with the exception. Empty catch blocks are forbidden in review. Verify counters by tests — the emitter counter *lied* (0 dropped): add a regression test asserting it increments when a row is dropped.

- **Logs carry correlation and context, or they carry nothing**:Every log line is attributable to a unit of work (request/event/batch) through an id that threads the whole path, and carries enough context — component, step, key inputs, latency — to reconstruct the failure without code archaeology. — **Why**:Real incident (Ava, 2026-08): a WARNING `query cancellation failed: cancellation timeout expired` fired 404 times in 7 days, concentrated in rollout windows, and its source was in *no* repo branch — un-attributable, hence un-actionable; the only fix was a suppression hack. Thomas & Hunt's debugging laws: "read the damn error message" — an error message without the input, the state, and the step that failed cannot be read. — **How**:Every log record includes: a correlation id (request/event/agent id), the component and step, bounded key inputs, and outcome latency. Log entry/exit with timing on anything non-trivial. Bound volume: default verbosity fits in one screen over an incident window; debug detail lives behind a knob — log spam that drowns the signal is the same failure at the other extreme.

- **Metrics are contracts**:If a resource is bounded, a rate can imbalance, or an operation can fail, it must have a number — queue depth, drop counts, retry rate, error rate, high-water marks — published and trendable. — **Why**:Real incident (Ava SSE, 2026-08): 318K events dropped in a day because emit rate (50–100/s) outran drain rate (~0.5/s over a degraded link); the 2048-deep queue filled in minutes. It was a rate imbalance, not a crash — only numbers (drain vs emit rate, depth over time) could expose it early; the drop semantics (newest-first) were the worst possible for a live view. A queue with a size limit and no depth metric is a bomb with no fuse. — **How**:For every bounded resource: emit depth/usage, overflow count, and the drop policy. For every retry loop: emit attempt count, success-after-retry, and give-up count. Numbers are monotone counters or gauges with units — "status: ok" strings do not count. The overflow counter increments exactly where something is dropped, and an alert fires when it moves.

- **The critical path is visible end-to-end: "which step" beats "how slow overall"**:Every request or event crossing subsystem boundaries carries per-step timing (a trace, or time-ordered step logs), so "which step got slow" is answerable from records. — **Why**:Real incident (Ava metrics page, 2026-07): the "obvious" suspect — a missing index (310ms seq scan) — was fine; the real bottleneck was 128MB of JSONB payload crossing the wire plus 576K dicts deserialized in Python. The 2.3× speedup came only from per-step measurement (SQL → wire → deserialize → render). Without step timing, "it's slow" sends you to fix the wrong layer. — **How**:For each critical path, record per-step durations under the correlation id, or instrument spans, so "which step got slower?" is answerable from records alone.

- **Alert on silence, not just on crashes**:A crash is loud; the absence of an expected event is silent. Watchdogs invert the alarm: if a scheduled event (heartbeat, report, batch completion, CI verdict) does not arrive on time, that is an incident. Health checks must exercise the real path. — **Why**:Real incidents (Ava, 2026-08): a CI watcher failed inside a bare except and never reported — the failure of *expected reporting* went unnoticed until the user found it; a Redis client's `is_connected` stayed True on a dead transport, so PING-based "health" never exercised the real write path and a process wrote into a dead socket for 45 minutes before crashing. A liveness check that does not touch the resource it claims to check is theater. — **How**:For every expected event, define the deadline (N × expected interval) and a watchdog that fires on absence. Health checks do a real round trip through the actual data path — a read, a write, a publish. Watch the work product (rows written, events delivered), not just the process (thread alive).

- **Observability is a design input, not an afterthought**:Before a new system goes live, the design answers "how will I know when it fails" for each failure class — with a signal, a place it lands, an owner who reads it, and an alarm for the absence case. — **Why**:Every incident above shares one root: the observable surface was designed after the failure, not before. Ousterhout: complexity accumulates incrementally and bites invisibly (§1.4) — an un-instrumented subsystem fails as "mystery". The right counters cost minutes at design time; retrofitting them after a silent failure costs an investigation plus a postmortem. — **How**:Design reviews ask, per component: what are the failure classes, and what is the observable signal for each? New components ship with at least one metric, one structured log line, and one watchdog — or they do not ship.
- **Know what your metric measures**:Before trusting a number, answer "what exactly does this instrument measure?" — a metric that measures the wrong object makes incidents invisible. — **Why**:Layer-1 behavioral eval (2026-08-06, t3): `publish_latency` was measuring handler-internal time (≈ the embedding call), not end-to-end delivery — so an 8–40s real latency was invisible in the metrics while the incident raged. The same blind spot hides "queue full" when the published metric is "items pushed". — **How**:For every key metric, write down its measurement boundary (from where to where) in the design doc; when an incident's symptom contradicts a healthy metric, suspect the metric's object before suspecting the system.

## Checklist

- [ ] **MUST** Every exception path either re-raises or (at a recovery boundary) logs + increments a drop/error counter: no bare `except: pass` / `contextlib.suppress` anywhere?
- [ ] **MUST** Every dropped/rolled-back/given-up item increments a visible counter, logs the reason, and has a test asserting the counter moves?
- [ ] **MUST** Every log line carries a correlation id and enough context (component, step, key inputs, latency) to locate its source without grep archaeology?
- [ ] **MUST** Every bounded resource (queue, pool, buffer, connection) publishes depth/usage and an overflow count, with an alert on sustained overflow?
- [ ] **MUST** The critical path has per-step timing (trace or time-ordered step logs) so "which step got slow" is answerable from records?
- [ ] **MUST** Every expected-but-optional event (batch completion, heartbeat, report, verdict) has a watchdog that alerts on its absence within N × expected interval?
- [ ] **MUST** Health checks exercise the real data path (a real round trip), not a liveness flag or a cached state?
- [ ] **MUST** For every key metric: is its measurement boundary written down (from where to where), and does it measure the thing users actually experience (e.g. end-to-end delivery, not handler-internal time)?
- [ ] **SHOULD** The design doc answers "how will I know when it fails" per failure class, with an owner for each signal?
- [ ] **SHOULD** Log volume is bounded: default verbosity fits an incident window; debug detail sits behind a knob?
- [ ] **SHOULD** Counters are monotone with units, and no "status: ok" string substitutes for a number?

## Anti-Patterns

- **The suppressed batch**:wrapping a batch write in `contextlib.suppress(Exception)` / an empty catch — one bad row rolls back the whole batch with zero trace. → alternative: isolate per-row failures with counters + logs, and let unknown errors crash.
- **The lying counter**:a drop counter that lives inside the suppressed block and never increments — "0 dropped" becomes false assurance. → alternative: increment at the point of failure, outside the suppression; prove it with a regression test.
- **The endless silent retry**:a retry loop that swallows the error and never reports give-up — each retry amplifies load while hiding the cause. → alternative: bounded retries, attempt counters, a logged give-up, and an alert on give-up rate.
- **The theatrical health check**:a liveness flag or PING that never exercises the real path — `is_connected=True` on a dead transport. → alternative: health checks do a real round trip through the actual resource and data path.
- **The un-attributable warning**:a log line with no source id, no context, and no owner — hundreds of copies of a message nobody can locate. → alternative: correlation id + component + bounded inputs, and a rule that unknown-source warnings are themselves filed as debt.
- **Monitoring the process, not the work**:checking "thread alive / process up" while the queue silently drains to zero. → alternative: watch the work product — rows written, events delivered, queue depth — and alarm on absence.

## Examples(bad → good)

**Example 1: bare print vs correlated structured logging**

❌ Bad:
```python
def handle_event(event):
    try:
        process(event)
        print("processed", event.id)   # no correlation, no timing, no context
    except Exception:
        print("failed")                 # no id, no reason, no counter, no alert
```

✅ Good:
```python
log = logging.getLogger("events")

def handle_event(event):
    t0 = time.monotonic()
    try:
        process(event)
    except Exception as exc:
        DROPPED.inc()                   # counter increments at the point of failure
        log.error("event processing failed",
                  extra={"correlation_id": event.corr_id, "event_id": event.id,
                         "reason": repr(exc),
                         "latency_ms": (time.monotonic() - t0) * 1000})
        raise                           # unknown errors crash — never bare-swallow
    log.info("event processed",
             extra={"correlation_id": event.corr_id, "event_id": event.id,
                    "latency_ms": (time.monotonic() - t0) * 1000})
```

**Example 2: the silent batch rollback vs honest per-row accounting**

❌ Bad (the emitter incident pattern):
```python
with contextlib.suppress(Exception):    # one FK violation → WHOLE batch rolls back
    for row in batch:
        db.execute(row)                 # dropped stays 0, no log, "thread alive"
```

✅ Good:
```python
ok = 0
for row in batch:
    try:
        db.execute(row)
        ok += 1
    except IntegrityError as exc:
        DROPPED.inc()                   # honest counter, at the failure point
        log.warning("row dropped", extra={"row_id": row.id, "reason": str(exc)})
    except Exception:
        raise                           # unknown errors are never silent
BATCH_ATTEMPTED.inc(len(batch))
BATCH_OK.inc(ok)
```

**Example 3: theatrical health check vs real-path health check**

❌ Bad:`is_connected` returns True while the socket underneath is dead — the PING never exercises the real write path; a process writes into a dead transport for 45 minutes, then dies with an uncaught error type.

✅ Good:health checks do a real round trip through the actual data path with a timeout; liveness is transport-aware (`transport.is_closing()` ⇒ reconnect); a watchdog alerts when the heartbeat of *work flowing* (events delivered) does not arrive — not just when the process crashes.

## Relationships

- `principles/error-handling` — error-handling defines the error model (define errors out of existence, crash early, handle at the right layer); observability is its observation surface: every "handled" error must leave a trace. A swallowed error is invisible to error-handling and to observability alike — the two skills share the same enemy.
- `practices/performance` — performance owns capacity, baselines, and measurement; observability consumes its numbers. The queue-depth/drop metrics that expose a rate imbalance are the same counters performance budgets; "which step got slow" requires performance's per-step measurement discipline.
- `practices/concurrency` — concurrency owns backpressure and connection lifecycles; observability makes backpressure visible. The dead-transport incident is a connection-lifecycle failure whose *detection* is an observability problem; the SSE queue-full incident is a backpressure failure that only numbers could expose early.
- `ai-era/verification-discipline` — verification of AI-generated code includes observability verification: every AI-written feature ships with its metric, log line, and watchdog, and tests must verify the counters themselves (the emitter fix's regression test asserts the batch survives *and* the counter is honest). Observability is the production half of verification.
- `practices/maintenance` — the observable surface rots like any other code: log spam, dead alerts, un-attributable warnings (the query-cancellation incident) are technical debt and need the same broken-window repair and debt tracking.
- `references/03-pragmatic-programmer.md` §4.2 — the six iron laws of debugging: observability is what makes laws 3–6 possible (reproduce the failure from records, read the error message, don't assume — prove from the data).

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
- Thomas & Hunt, *The Pragmatic Programmer* — the six iron laws of debugging; Tip 62 "don't program by coincidence". See `references/03-pragmatic-programmer.md` §4.2.
- Ousterhout, *A Philosophy of Software Design* — complexity accumulates invisibly (§1.4); the most expensive failures are the ones you cannot see. See `references/01-philosophy-of-software-design.md`.
- **Layer-1 behavioral eval (2026-08-06)** — t3: publish_latency measured the wrong object (handler-internal time, not end-to-end latency), caught by the blind judge(`research/eval/ab/judge-verdict-t3.md`)
