# Database Write Batch Audit + Best Practice

Date: 2026-07-07
Auditor: #1547
Client: #405

> **Superseded (2026-08-04, event-system W1):** the loguru Postgres sink it
> audits was replaced by the unified event emitter (`shared/telemetry.py`) —
> one bounded queue + drain thread now batch-writes the `events` table and the
> legacy `agent_events`/`event_log` mirrors. The audit's conclusions (batch,
> bound the queue, shed-and-count) carried over verbatim; its per-symbol
> references (`_postgres_sink`, `_ThreadedPostgresSink`) are historical.

## Audit Scope

Reviewed all database write paths in the `~/Ava` project, covering the following modules:

| Module | File |
|------|------|
| event_log (audit events) | `shared/audit_events.py` (unified `events` stream since the 2026-08 rework) |
| agent_events (loguru PG sink) | `shared/log.py` |
| agent_activity | `ava_builtins/plugins/ava_fleet/plugin.py:log()` |
| inbound_messages | `shared/db.py`, `agent/db.py`, `services/heartbeat/daemon.py` |
| agents_meta | `agent/db.py`, `ava/self.py` |
| agent_tasks | `ava_builtins/plugins/ava_fleet/task_registry.py` |
| agent_notices | `ava_builtins/plugins/ava_fleet/plugin.py:notify()` |
| LangGraph checkpoints | `AsyncPostgresSaver` (per-turn) |
| machines / schedules / presets | `shared/machines.py`, low frequency |

---

## 1. Per-Path Audit

### 1.1 event_log (`shared/event_log.py`)

**Write pattern**: Each call to `insert_event_log()` / `insert_event_log_async()` produces a single `INSERT INTO event_log ... RETURNING id`.

**Callers** (all callers pass their own cursor; the event INSERT and the operation's write occur in the same transaction):

| Caller | Trigger frequency |
|--------|---------|
| `shared/db.py:insert_inbound_message()` | Every inbound message |
| `shared/db.py:insert_compact_request_inbound()` | Low frequency (user manually initiates compact) |
| `agent/db.py:mark_agent_status()` | Every status switch (running↔idling) |
| `agent/_starting.py:claim_agent_row()` | Every agent start |
| `ava/self.py:compact()` | Low frequency |
| `ava_builtins/plugins/ava_fleet/task_registry.py:create()/update()` | Low frequency |
| `ava_builtins/plugins/ava_fleet/plugin.py:log()` | Once per turn |
| `ava_builtins/plugins/ava_fleet/plugin.py:set_label()` | Extremely low frequency |
| `agent/hooks/compact.py` auto-compact | Low frequency (triggered by token threshold) |

**Batch assessment**: ✅ **Superseded (2026-08)**: the unified event rework removed the caller-transaction contract — `shared/audit_events.py` now ships `insert_event_log_many()` and the emitter's drain batch is the single write path (the "INSERT rides in the caller's transaction" contract is deliberately gone; see `shared/audit_events.py` docstring).

---

### 1.2 agent_events — loguru PostgresSink (`shared/log.py`)

**Write pattern**: Each loguru log record → `_postgres_sink()` → `INSERT INTO agent_events (...)`.

**Architecture**: Already has a decoupling layer—`_ThreadedPostgresSink` (`queue.Queue` + daemon thread), loguru's `write()` is a non-blocking `queue.put()`.

```
loguru logger.info(...)
    → _ThreadedPostgresSink.write(message)    # non-blocking enqueue
        → daemon thread: _drain()
            → _postgres_sink(message)          # one INSERT each
```

**Batch assessment**: ✅ **Suitable for batching, and yields the greatest benefit**.

The current `_drain()` calls `_postgres_sink(message)` one by one, each requiring a separate `pool.connection()` → `INSERT` → release. Since `_ThreadedPostgresSink` is already an asynchronous decoupling layer, accumulating a batch in the drain thread is a natural extension:

- **Current**: N log entries = N round-trips + N connection borrows
- **After modification**: N log entries ≈ ceil(N/B) round-trips, where B is the batch size

**Note**: `_postgres_sink` processes each message with `record = message.record` + `extra` dict processing. Switching to batching requires extracting the "message → parameter tuple" logic into a pure function, then using batch `executemany()` or `INSERT ... VALUES (...), (...), ...`.

**Risk**:
- `_postgres_sink` currently performs individual INSERTs on a pool connection, each with independent exception handling (`contextlib.suppress(Exception)`). Batching requires deciding: if one row fails, discard the entire batch? Recommendation: catch per-row and use `executemany` + savepoint.

---

### 1.3 ~~agent_activity + event_log dual-write~~ (obsolete)

`ava.self.log` was removed (2026-08-02, SDK alignment); this section is kept only
for the write-pattern history. The pattern it described:

```python
# 1. INSERT INTO agent_activity
cur.execute("INSERT INTO agent_activity ...")
# 2. INSERT INTO event_log (via insert_event_log)
insert_event_log(cur, event_type="report_activity", ...)
# 3. publish_agent_updated_sync (Redis)
```

**Batch assessment**: ⚠️ Optimizable but low benefit. Both INSERTs share the same cursor/transaction, and each has a small payload. The dual-write itself is not a problem—it's by design (agent_activity is a fast query table, event_log is a unified audit table). Could consider using a CTE to combine into a single round-trip:

```sql
WITH activity AS (
    INSERT INTO agent_activity (...) VALUES (...) RETURNING id
)
INSERT INTO event_log (...) VALUES (...)
```

But the benefit is negligible (saves one round-trip, but these two INSERTs already execute sequentially on the same connection, so the overhead is negligible). **Not a priority.**

---

### 1.4 inbound_messages (`shared/db.py`, `agent/db.py`)

**Write pattern**:

| Operation | Pattern | Frequency |
|------|------|------|
| `insert_inbound_message()` | Single INSERT + event_log | Every inbound message |
| `signal_live_agents_restart()` | Batch `INSERT ... SELECT` ✅ Already batched | `ava update` |
| `claim_inbound_batch()` | Batch `UPDATE ... WHERE id IN (...)` ✅ Already batched | Every claim |
| `reconcile_claimed_inbounds()` | Batch UPDATE ✅ Already batched | Every startup |
| `_send_heartbeat_checkin()` (was `_send_heartbeat_nudge`) | Single INSERT (per agent) | Heartbeat cycle |

**Batch assessment**: Core paths are already batched. Only `_send_heartbeat_checkin()` still has optimization potential:

The **heartbeat daemon** may discover N agents needing a nudge within one poll cycle, currently inserting one by one:
```python
for agent_id, idle_minutes in rows:
    _send_heartbeat_checkin(pool, agent_id, idle_minutes)  # each: 2 INSERTs
```

**Optimization plan**: Combine into a single batch INSERT (`INSERT INTO inbound_messages (...) VALUES (...), (...), ...` + same batch for `agent_events`). Benefit: when there are M idle agents, reduces from 2M round-trips down to 2.

---

### 1.5 agents_meta (`agent/db.py`)

**Write pattern**: CAS UPDATE (`WHERE id = %s AND status = %s`), one per status switch.

**Batch assessment**: ❌ Not suitable. The state machine requires atomic CAS, cannot batch.

---

### 1.6 agent_tasks (`ava_builtins/plugins/ava_fleet/task_registry.py`)

**Write pattern**: Low-frequency operations. `create()` = 1 INSERT + 1 event_log, `update()` = 1 UPDATE + 1 event_log.

**Batch assessment**: ❌ Not needed. Low-frequency, serial operations.

---

### 1.7 LangGraph Checkpoints (`AsyncPostgresSaver`)

**Write pattern**: At the end of each turn, LangGraph internally writes checkpoints (`aput` / `aput_writes`).

**Batch assessment**: ❌ Managed internally by LangGraph, outside this project's control. One checkpoint per turn is by design, and `durability="exit"` is already the optimal configuration.

---

## 2. Best Practice Plan

### 2.1 Batch Size Thresholds

| Path | Recommended batch size | Rationale |
|------|----------------|------|
| agent_events (PostgresSink) | 50 entries or 500ms | Logs are high-frequency but latency-insensitive; 50 entries ≈ 1 pg round-trip vs 50 |
| heartbeat nudge | Unlimited (whole batch) | Low frequency (every 5 min), full batch processing within one poll cycle |

### 2.2 Flush Strategy

**Dual trigger: time-based + count-based**:

```
If batch accumulation ≥ BATCH_SIZE → flush immediately
Else if time since last flush ≥ FLUSH_INTERVAL_MS → flush
On process exit → fallback flush (atexit / tasks_to_complete)
```

- `BATCH_SIZE = 50` (agent_events)
- `FLUSH_INTERVAL_MS = 500` (agent_events)

### 2.3 Process Exit Fallback

`_ThreadedPostgresSink` already has `stop()` (sends sentinel) and `tasks_to_complete()` (synchronously drains when loguru calls `logger.complete()`). After modification:

- `stop()`: Sends sentinel → drain thread flushes remaining batch before processing sentinel
- `tasks_to_complete()`: Synchronously drains queue + flushes remaining batch (preserving existing semantics)

### 2.4 Error Handling

agent_events is a best-effort log pipeline (data loss is acceptable—JSONL files serve as durable backup). When a row fails in a batch INSERT:

- **Approach A (Recommended)**: Discard entire batch + WARNING log. Simple, matches current "drop on failure" semantics.
- **Approach B**: `executemany` + per-row savepoint. More granular but higher complexity.

Recommend Approach A—the contract of agent_events is best-effort, and this path already has `contextlib.suppress(Exception)`.

---

## 3. Priority Ranking

| Priority | Path | Effort | Benefit |
|--------|------|--------|------|
| **P0** | agent_events `_ThreadedPostgresSink._drain` batch INSERT | ~30 lines | **Highest-frequency path**: each agent produces 5–30 log entries per turn, accumulated across the cluster. Batching can reduce INSERT count by 50x. |
| P1 | heartbeat daemon `_send_heartbeat_checkin` batching | ~20 lines | Low frequency but simple; one poll cycle may nudge multiple agents |
| P2 | `plugin.py:log()` CTE combined dual-write | ~10 lines | Minimal benefit, saves only 1 round-trip |
| P3 | audit-event batching | ~~Not recommended~~ shipped as `insert_event_log_many` | See §1.1 superseded note |

---

## 4. agent_events Batch Modification Draft

### Existing Code (`shared/log.py`)

`_drain()` consumes one by one:

```python
def _drain(self) -> None:
    while True:
        message = self._queue.get()
        if message is None:
            break
        with contextlib.suppress(Exception):
            self._writer(message)
```

### Modification Plan

1. Add new `_postgres_sink_batch(messages: list)` — batch INSERT
2. Modify `_drain()` to a batch accumulation loop:

```python
def _drain(self) -> None:
    import time
    batch = []
    last_flush = time.monotonic()
    while True:
        try:
            message = self._queue.get(timeout=0.5)
        except queue.Empty:
            # Timeout → flush existing batch
            self._flush_batch(batch)
            batch = []
            last_flush = time.monotonic()
            continue
        if message is None:  # sentinel
            self._flush_batch(batch)
            break
        batch.append(message)
        if len(batch) >= 50 or (batch and time.monotonic() - last_flush >= 0.5):
            self._flush_batch(batch)
            batch = []
            last_flush = time.monotonic()
```

3. `_flush_batch(batch)` constructs a multi-row INSERT:

```sql
INSERT INTO agent_events (ts, agent_id, level, event, payload)
VALUES (%s, %s, %s, %s, %s::jsonb),
       (%s, %s, %s, %s, %s::jsonb),
       ...
```

4. Extract the message → parameter logic from `_postgres_sink` into a pure function `_message_to_params(message) -> tuple`

### Risks

- **JSONL file backup unaffected** (separate loguru sink, independent of the PG sink)
- **agent_events queries unaffected** (same table, same rows)
- Batch INSERT's `jsonb` parameters need `json.dumps()` serialization for each payload (`psycopg`'s `%s::jsonb` accepts a string)

---

## 5. Decisions Log

- **No event_log batching**: event_log INSERT shares a transaction with business writes; batching would break transaction boundaries
- **No agents_meta batching**: CAS UPDATE requires atomicity
- **No LangGraph checkpoint batching**: Managed internally by the framework
- **agent_events batching is a clear net gain**: Highest-frequency path, existing decoupling layer, isolated change
