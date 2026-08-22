---
name: performance
description: "Use when designing or reviewing anything with a load, latency, or resource budget — queries, endpoints, event loops, batch jobs. Covers measurement-first discipline, critical-path design, and the capacity mistakes that turn into production incidents."
---

# Performance

## One-Sentence Core
> Performance is designed, not tuned: identify the critical path, make it the shortest path, and let measurement — never intuition — decide what to optimize.

## Core Principles

- **Measure before optimizing**:Programmers' intuition about bottlenecks is highly unreliable. — **Why**:Ousterhout's RAMCloud case: a refactor driven by profiling doubled core-path speed and cut code by 20%; intuition-driven "optimization" routinely targets the wrong layer. — **How**:Profile the actual system before changing anything; record the baseline number, the target, and the measured delta in the same PR as the change.

- **Design around the critical path**:Strip every exception branch and special case out of the happy path — the minimal code the common request must execute decides latency. — **Why**:Every branch on the hot path costs time even when not taken; Ousterhout: merge special cases into one or two checks at the path's start, branching out only on failure. — **How**:For each endpoint/operation, write down the minimal happy-path sequence; anything not on it moves behind a guard at the start.

- **Bound every query and materialization**:Unbounded reads are deferred memory explosions. — **Why**:Real incident: one /api/metrics call materialized 230K rows/day with no LIMIT — +47MB permanently resident per call (gateway memory profile, 2026-08). — **How**:Every read that can grow with data volume gets an explicit LIMIT/page size, a cursor, or an aggregation; check call sites in review, not just the query.

- **Blocking work never runs on the event loop**:Synchronous heavy work on an async loop freezes everything behind it. — **Why**:Real incident: a synchronous embedding call on the gateway event loop froze all HTTP for 5-18s (loading-conversation rootcause #671). — **How**:Anything that can take more than a few ms (IO, embeddings, searches) runs off-loop — thread pool, worker, or async-native client; review asks "does this block the loop?".

- **Cost is a design input, not an afterthought**:If a feature's steady-state cost (CPU, memory, tokens, rows) is unbounded or unmeasured, it is a production incident waiting. — **Why**:Every capacity incident in the bug log (SSE queue fill, metrics materialization, recall storms) was an un-budgeted steady-state cost. — **How**:For each feature: what is its per-request cost, its daily volume, and its growth rate? Budget them explicitly in design.

## Checklist
- [ ] **MUST** Is there a measured baseline and a target for this change's latency/throughput — or is it being shipped on intuition?
- [ ] **SHOULD** Does the happy path contain exception branches or special-case handling that could move behind guards?
- [ ] **MUST** Every unbounded read (no LIMIT, no cursor, no aggregation) flagged and bounded?
- [ ] **MUST** Is there any synchronous heavy work (IO, compute, search) on an event loop or request thread?
- [ ] **MUST** Is the steady-state cost of this feature (rows/day, memory, tokens, connections) estimated and budgeted?
- [ ] **SHOULD** Would this design survive ~10x the current volume without redesign (heuristic — scale it to the business's real growth bound)?
- [ ] **SHOULD** Is there a load test or benchmark that would catch a 2x regression in the critical path?
- [ ] **SHOULD** Are expensive operations cached or memoized where repetition is predictable?

## Anti-Patterns
- **Premature optimization**:optimizing before measuring → alternative: profile first, then optimize the measured bottleneck
- **The unbounded SELECT**:`fetchall()` on a table that grows daily → alternative: LIMIT + cursor + aggregation
- **The synchronous event-loop call**:embedding/DB/HTTP call inside an async handler → alternative: off-loop execution
- **The materialized metric**:building a huge in-memory structure for every request → alternative: streaming, sampling, pre-aggregation
- **Caching everything**:caching without invalidation strategy → alternative: cache with TTL + invalidation events

## Examples(bad → good)
- **Bad**: `rows = db.fetchall("SELECT * FROM events WHERE ts > ?")` on a hot endpoint — unbounded, grows daily.
  **Good**: `rows = db.fetch_page("SELECT * FROM events WHERE ts > ? ORDER BY ts DESC LIMIT 50", cursor=last_id)` — bounded, paginated.
- **Bad**: `embeddings = model.embed(query)` called synchronously inside an async route handler — freezes the loop.
  **Good**: the embedding call goes to a worker thread/queue; the handler awaits a future or streams progress.

## Relationships
- `practices/concurrency` — **Division of labor**: performance = capacity and measurement (throughput, latency, resource budgets, critical path); concurrency = execution flows and shared state (correctness across threads/async/processes, connection lifecycles, backpressure). The two share the same incidents (event-loop blocking, full queues), but performance asks "is it fast / cheap enough" and concurrency asks "is it correct under concurrency". Performance changes touching shared resources should read concurrency first
- `principles/complexity-management` — complexity and performance both favor small interfaces and clear structure; a hot path is a kind of critical module
- `principles/error-handling` — timeouts/retries are the error-handling side of capacity ("transient errors at the low layer")
- `practices/design` — scarce-resource budgeting in design checklists is the design-time half of this skill
- `ai-era/verification-discipline` — load tests and benchmarks are the verification half of performance claims

## Sources
- references/01-philosophy-of-software-design.md §4.5 (Performance and the Critical Path)
- Ava incident records: gateway-mem-profile (fetchall without LIMIT, +47MB), metrics-perf-fix, loading-conversation-rootcause (event-loop blocking), sse-queue-full (backpressure)
