---
type: doc
title: Shared — infrastructure utilities
description: Postgres/Redis client wrappers, the transaction-level client message identity in chat_delivery, the never-raise publish primitive for lifecycle events, the agent kernel's streaming live events, and the long-lived Redis pub/sub listener.
tags: []
---

# Shared — infrastructure utilities

- **Infrastructure utilities** (`shared/db.py`, `shared/chat_delivery.py`, `shared/redis_client.py`, `shared/pg_*.py`): Postgres/Redis client wrappers; `shared/chat_delivery.py` owns the transaction-level client message identity (unique key + immutable body/agent/source comparison + stable inbound receipt), closing the commit/HTTP-response crash gap that a response cache cannot. Initial inserts may attach [[shared/inbound-provenance.ava.okf.md|server-owned inbound provenance]]; those audit facts are retained but deliberately excluded from retry conflict decisions. `shared/redis_client.py:publish_best_effort`/`_sync` is the never-raise publish primitive for lifecycle events (`mark_agent_status`, agent_events, labels, ops lifecycle) — fire-and-forget, on failure it only degrades with a log, never throws upwards. Async publish attempts carry an operation-level bound so a half-open Redis health-check read cannot hold a lifecycle caller; the shared client's socket timeout remains unbounded because long-lived pub/sub reads legitimately idle. The agent kernel's **streaming live events** (chat_start/chat_delta/code_*/exec_*, the live view's main traffic) go through `shared/event_publisher.py`'s `AgentEventPublisher` instead (non-blocking enqueue, since 2026-06-04 #806). `shared/redis_listener.py` is the long-lived Redis pub/sub listener with auto-reconnect/resubscribe (PG LISTEN/NOTIFY → Redis pub/sub rework) that backs the claim node's inbound idle-wait (`wait_for_inbound`); the in-turn interrupt watcher polls the DB instead of sharing this listener (see `agent/graph/_interrupt.py`).

Parent: [[shared/shared.ava.okf.md|shared libraries]].
