---
type: doc
title: Base — infrastructure utilities
description: Postgres/Redis client wrappers, the transaction-level client message identity in chat_delivery, the never-raise publish primitive for lifecycle events, the agent kernel's streaming live events, and the long-lived Redis pub/sub listener.
tags: []
---

# Base — infrastructure utilities

- **Infrastructure utilities** (`base/db/__init__.py`, `base/agents/messages/chat_delivery.py`, `base/events/live/redis_client.py`, `base/pg_*.py`): Postgres/Redis client wrappers (pooled Postgres sessions also enforce the [[base/db/docs/code-version-gate.ava.okf.md|code version gate]]); `base/agents/messages/chat_delivery.py` owns the transaction-level client message identity (unique key + immutable body/agent/source comparison + stable inbound receipt), closing the commit/HTTP-response crash gap that a response cache cannot. Initial inserts may attach [[base/agents/messages/docs/inbound-provenance.ava.okf.md|server-owned inbound provenance]]; those audit facts are retained but deliberately excluded from retry conflict decisions. `base/events/live/redis_client.py:publish_best_effort`/`_sync` is the never-raise publish primitive for lifecycle events (`mark_agent_status`, agent_events, labels, ops lifecycle) — fire-and-forget, on failure it only degrades with a log, never throws upwards. Async publish attempts carry an operation-level bound so a half-open Redis health-check read cannot hold a lifecycle caller; the shared client's socket timeout remains unbounded because long-lived pub/sub reads legitimately idle. The agent kernel's **streaming live events** (chat_start/chat_delta/code_*/exec_*, the live view's main traffic) go through `base/events/live/publisher.py`'s `AgentEventPublisher` instead (non-blocking enqueue, since 2026-06-04 #806). `base/events/live/redis_listener.py` is the long-lived Redis pub/sub listener with auto-reconnect/resubscribe (PG LISTEN/NOTIFY → Redis pub/sub rework) that backs the claim node's inbound idle-wait (`wait_for_inbound`); the in-turn interrupt watcher polls the DB instead of sharing this listener (see `agent/graph/interrupt.py`).

Parent: [[base/docs/base.ava.okf.md|base library]].
