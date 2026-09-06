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

- **Inbound message queue**: The `inbound_messages` table is the unified agent wake-up entry point, with 11 `kind` types:
  - `chat` — User/peer agent chat messages (the only kind that goes through two-stage `claimed`, initiating multi-step LLM+exec)
  - `system_note` — Framework message rendered as a tagged system marker; a permanently blocked descendant reports only classifier metadata to its nearest alive immutable `born_spawner` ancestor (falling back to `spawner` for pre-migration rows) through this path
  - `compact_summary` — Written by agent `ava.self.compact()`, claim directly replaces messages
  - `compact_request` — Triggered by UI `/compact`, claim runs backend LLM to generate summary and replace
  - `heartbeat` — Heartbeat daemon check-in for idle agents
  - `cancel` — Pause instruction; claim processes via `case InboundKind.CANCEL`: discards in-flight LLM generation; if no new `chat` in the same batch, returns to idle (no lifecycle marker); if a new `chat` is present in the same batch, that chat is treated as a fresh intent, directly waking to before_llm (`halted=False`) instead of idle—avoiding the situation where "message is submitted but agent never responds"
  - `terminate` — Termination instruction; claim appends lifecycle marker + goto END
  - `restart` — Claim only marks RESTARTING + goto END (no message appended)
  - `restart_completed` — After the durable restarter respawns the process, the new process appends a "You have been restarted" marker; `respawn_agent` is its sole writer
  - `resurrect` — After resurrection, the new process appends a marker
  - `fork` — Fork identity marker
- **In-flight interrupt (not via claim)**: `has_pending_interrupt` (`agent/db.py:306`) performs a read-only peek on an additional channel—limited to `status='pending'` and `kind ∈ {cancel, terminate}` and `source ≠ 'self'`—to allow in-flight llm/exec nodes to instantly abort the current action; **dispatch semantics remain exclusive to claim** (all 10 kinds are dispatched in claim, cancel→idle / terminate→END)
- **Redis pub/sub wake-up**: Each INSERT publishes via `insert_inbound_message` to `<prefix>:inbound:<agent_id>` (`shared.cluster.inbound_channel`); the agent's `RedisInboundListener` blocks on it, avoiding polling
- **Provenance compatibility**: agent-side queue writers remain unchanged and leave the nullable gateway credential, transport, content-hash, and source-assertion columns `NULL`; absence means legacy or no gateway credential boundary, not a failed verification
- **Connection pool management**: `AsyncConnectionPool(max_size=2)` shared between the LangGraph checkpoint saver and kernel SQL
- **Health check**: The pool's `check_connection` runs `SELECT 1` on each connection acquisition, with transparent reconnection

## Key Dependencies

- [[loop.ava.okf.md]] — The agent main loop is the creator and consumer of the db pool and listener; LangGraph checkpoint saver shares the same `AsyncConnectionPool` with kernel SQL
- [[graph.ava.okf.md]] — The claim node consumes the inbound queue and dispatches

## Entry Points

- `agent/db.py:wait_for_inbound(pool, listener, agent_id, timeout_s)` — Blocking wait for inbound: first a SELECT, then `listener.wait_one` blocks until Redis publish or timeout
- `shared/redis_listener.py:RedisInboundListener.wait_one(timeout)` — Subscribes to `<prefix>:inbound:<agent_id>` (`shared.cluster.inbound_channel`) and waits for a publish (auto-reconnect + re-subscribe); timeout only **silently returns**; fallback SELECT recheck for publish-loss is in the `wait_for_inbound` loop (`agent/db.py:_has_pending_inbound`), not inside `wait_one`
- `agent/db.py:claim_inbound_batch(...)` — Borrows with an at-most-5-second pool-acquisition ceiling and installs the same transaction-local row-lock ceiling, then locks `agents_meta` before inbound rows in one write transaction. A timeout rolls the transaction back and returns the connection; process mode pauses/retries through its DB recovery boundary, while hosted scheduling drops the round for a later durable wake/scan retry. `agent/inbound_ownership.py` checks the admitted generation/owner and a fresh lease after the lock wait; stale or missing ownership tokens cannot claim owned rows. An unowned consumer with pending restart/terminate fails before acknowledging any batch. Ordinary legacy chat and summary remain compatible: chat → `claimed`, other non-lifecycle kinds → `done`. Any successful ordinary batch claim clears `agents_meta.wake_suppressed_until` and `wake_suppress_reason` in the same transaction, proving the recovered process can consume queued work.
- `agent/db.py:renew_agent_lease(pool, agent_id)` — re-arms the agent's liveness lease (`lease_expires_at = now() + TTL`, scoped to the alive statuses); driven by the run loop's renewal task ([[lease.ava.okf.md|Agent Liveness Lease]])

## Notes

- [[agent/db/lifecycle-recovery.ava.okf.md|Lifecycle recovery]] owns process/hosted completion evidence, force supersession, attempt confirmation and bounded durable user wakes.

- An owned lifecycle command dispatches alone with an internal receipt derived from its locked same-agent pointer and current generation/owner. Legacy latest-wins and pending-message vetoes cannot override this accepted command. Other rows remain pending; a real successor admission observes the restart before its next claim can consume them. The receipt is not effect authority: application still checks the fixed pointer and target incarnation, and acceptance never implies process exit.

- Claim, startup reconcile, compaction finalization and co-batch deferral use the same owner lock. Reconcile/finalization/deferral only mutate chat rows, never acknowledge lifecycle work using missing checkpoint anchors. The caller must retain existing single-flight ordering around checkpoint reads and compaction.
- Owned restart/terminate rows remain claimed through durable application and only become done on observed completion or explicit terminal failure/supersession. Non-lifecycle kinds retain their acknowledgement rules. Old binaries issuing unconditional SQL still require a verified shutdown/upgrade barrier; new columns alone cannot fence them.
- Connection budget: **2 PG connections per agent in steady state** (pool 2; the inbound listener uses Redis, not PG). Boot adds short-lived sync connections (schema gate, `assert_schema_current`, restart-completed write) — PgBouncer absorbs these bursts, so the 2-conn budget describes steady state only.
- Process restart installs a durable decision for the admitted incarnation; only the existing restarter launches its replacement. There is no agent-side atexit launcher, fallback CAS, grace poll or launch-on-DB-error path. Pausing the restarter leaves the accepted restart pending rather than bypassing the pause. External exit/launcher crash recovery still requires the fenced controller slice before activation.
- `RedisInboundListener` auto-reconnects and re-subscribes on disconnect to prevent publish loss
- The timeout is a safety net for publish loss (Redis restart/network glitches may drop publishes; if ACL denies subscription, falls back to pure SELECT recheck)
