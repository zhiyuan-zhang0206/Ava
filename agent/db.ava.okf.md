---
type: doc
title: Database Layer
description: Ava's Postgres persistence layer—responsible for agent message history storage, inbound message queue management, and agent wake-up mechanism. Core file
tags: []
---

# Database Layer

## What it is

Ava's Postgres persistence layer—responsible for agent message history storage, inbound message queue management, and agent wake-up mechanism. Core file `agent/db.py` (approx. 24KB) implements all asynchronous SQL operations on the kernel side.

## Core Responsibilities

- **Inbound message queue**: The `inbound_messages` table is the unified agent wake-up entry point, with 10 `kind` types:
  - `chat` — User/peer agent chat messages (the only kind that goes through two-stage `claimed`, initiating multi-step LLM+exec)
  - `compact_summary` — Written by agent `ava.self.compact()`, claim directly replaces messages
  - `compact_request` — Triggered by UI `/compact`, claim runs backend LLM to generate summary and replace
  - `heartbeat` — Heartbeat daemon check-in for idle agents
  - `cancel` — Pause instruction; claim processes via `case InboundKind.CANCEL`: discards in-flight LLM generation; if no new `chat` in the same batch, returns to idle (no lifecycle marker); if a new `chat` is present in the same batch, that chat is treated as a fresh intent, directly waking to before_llm (`halted=False`) instead of idle—avoiding the situation where "message is submitted but agent never responds"
  - `terminate` — Termination instruction; claim appends lifecycle marker + goto END
  - `restart` — Claim only marks RESTARTING + goto END (no message appended)
  - `restart_completed` — After respawn, the new process appends a "You have been restarted" marker
  - `resurrect` — After resurrection, the new process appends a marker
  - `fork` — Fork identity marker
- **In-flight interrupt (not via claim)**: `has_pending_interrupt` (`agent/db.py:306`) performs a read-only peek on an additional channel—limited to `status='pending'` and `kind ∈ {cancel, terminate}` and `source ≠ 'self'`—to allow in-flight llm/exec nodes to instantly abort the current action; **dispatch semantics remain exclusive to claim** (all 10 kinds are dispatched in claim, cancel→idle / terminate→END)
- **Redis pub/sub wake-up**: Each INSERT publishes via `insert_inbound_message` to `<prefix>:inbound:<agent_id>` (`shared.cluster.inbound_channel`); the agent's `RedisInboundListener` blocks on it, avoiding polling
- **Connection pool management**: `AsyncConnectionPool(max_size=2)` shared between the LangGraph checkpoint saver and kernel SQL
- **Health check**: The pool's `check_connection` runs `SELECT 1` on each connection acquisition, with transparent reconnection

## Key Dependencies

- [[loop.ava.okf.md]] — The agent main loop is the creator and consumer of the db pool and listener; LangGraph checkpoint saver shares the same `AsyncConnectionPool` with kernel SQL
- [[graph.ava.okf.md]] — The claim node consumes the inbound queue and dispatches

## Entry Points

- `agent/db.py:wait_for_inbound(pool, listener, agent_id, timeout_s)` — Blocking wait for inbound: first a SELECT, then `listener.wait_one` blocks until Redis publish or timeout
- `shared/redis_listener.py:RedisInboundListener.wait_one(timeout)` — Subscribes to `<prefix>:inbound:<agent_id>` (`shared.cluster.inbound_channel`) and waits for a publish (auto-reconnect + re-subscribe); timeout only **silently returns**; fallback SELECT recheck for publish-loss is in the `wait_for_inbound` loop (`agent/db.py:_has_pending_inbound`), not inside `wait_one`
- `agent/db.py:claim_inbound_batch(...)` — Locks `agents_meta` before inbound rows in one write transaction. `agent/inbound_ownership.py` checks the admitted generation/owner and a fresh lease after the lock wait; stale or missing ownership tokens cannot claim owned rows. Unknown legacy rows retain compatibility. Chat → `claimed`, others → `done`.
- `agent/db.py:renew_agent_lease(pool, agent_id)` — re-arms the agent's liveness lease (`lease_expires_at = now() + TTL`, scoped to the alive statuses); driven by the run loop's renewal task ([[lease.ava.okf.md|Agent Liveness Lease]])

## Notes

- Process lifecycle application fixes server-reserved `target_process_identity` in the command under the admitted owner/target lock. Its PID is the actual Python runtime, with OS birth evidence, not a DB timestamp or Windows redirector. Controller and explicit resurrection share termination observation: exact process disappearance/reuse permits completion and pointer clearing; live, unreadable or missing historical evidence defers. Swept session records do not erase this evidence. Hosted termination remains tied to real continuation settlement.
- Launch confirmation follows the exact boot-attempt record returned by the launcher, including the off-path spawn confirmation. It does not require canonical publication before admission; missing attempt evidence retains the hard boot deadline rather than guessing a dead process from a missing canonical name.
- A queued user wake arriving during old-process exit keeps its original inbound identity. The existing bounded resurrection caller persists a reserved blocked-terminate id and preparation-attempt count; it derives the fixed target from that original command and revalidates it under the actual preparation lock. Deadline is anchored to inbound creation, not HTTP redispatch. Exit-wait attempts do not expand launch budgets. Exhaustion or changed ownership leaves pending work with an explicit unresolved error, never a fabricated completion; no background scanner is added.

- An owned lifecycle command dispatches alone with an internal receipt derived from its locked same-agent pointer and current generation/owner. Legacy latest-wins and pending-message vetoes cannot override this accepted command. Other rows remain pending; a real successor admission observes the restart before its next claim can consume them. The receipt is not effect authority: application still checks the fixed pointer and target incarnation, and acceptance never implies process exit.

- Claim, startup reconcile, compaction finalization and co-batch deferral use the same owner lock. Reconcile/finalization/deferral only mutate chat rows, never acknowledge lifecycle work using missing checkpoint anchors. The caller must retain existing single-flight ordering around checkpoint reads and compaction.
- Owned restart/terminate rows remain claimed through durable application and only become done on observed completion or explicit terminal failure. Other non-chat kinds and genuinely unowned legacy rows retain their existing acknowledgement rules. Old binaries issuing unconditional SQL still require a verified shutdown/upgrade barrier; new columns alone cannot fence them.
- Connection budget: **2 PG connections per agent in steady state** (pool 2; the inbound listener uses Redis, not PG). Boot adds short-lived sync connections (schema gate, `assert_schema_current`, restart-completed write) — PgBouncer absorbs these bursts, so the 2-conn budget describes steady state only.
- Process restart installs a durable decision for the admitted incarnation; only the existing restarter launches its replacement. There is no agent-side atexit launcher, fallback CAS, grace poll or launch-on-DB-error path. Pausing the restarter leaves the accepted restart pending rather than bypassing the pause. External exit/launcher crash recovery still requires the fenced controller slice before activation.
- `RedisInboundListener` auto-reconnects and re-subscribes on disconnect to prevent publish loss
- The timeout is a safety net for publish loss (Redis restart/network glitches may drop publishes; if ACL denies subscription, falls back to pure SELECT recheck)
