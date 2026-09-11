---
type: doc
title: Agent Runtime
description: Hosted agent turns with durable identity and isolated code execution.
tags: []
---

# Agent Runtime

One `services.agent_host.daemon` runs each machine's local agents as asyncio
tasks. Agent identity, config, model state and checkpoint threads stay separate;
an idle agent has no running task. There is one runtime architecture and no
per-agent process launcher or runner-mode selector.

The host shares one compiled graph and logical `services/agent_host/pooled_checkpoint.py`
checkpointer. Each cursor leases its own pool connection and delegates pipeline,
transaction and cancellation cleanup to LangGraph's saver for that connection.
Unrelated agents can therefore read and write concurrently. The N-step wrapper
in `agent/startup.py` still serializes writes and flushes for the same thread.

Active agents have no default admission limit: `AVA_HOST_MAX_CONCURRENT_TURNS=0`
allows another agent to start while existing agents wait on models or tools.
A positive value opts into the host limit and queues excess continuations until
an admitted agent idles or exits. Per-agent single-flight and resource settlement
still apply. Active runtimes remain resident; completed turns immediately trim
the warm cache to its size/idle budget. Host memory, exec capacity and provider
quotas remain separate constraints.

New runners advertise zero-limit support in their bootstrap request. The gateway
projects zero to the legacy positive default for older runners, so partial
updates cannot turn their admission semaphore into a permanent zero-slot wait.

Database clients are independently bounded by `AVA_HOST_DB_POOL_MAX_SIZE`
(64 workload/checkpoint connections) and `AVA_HOST_CONTROL_POOL_MAX_SIZE`
(8 ownership/lifecycle/scan connections). Each pool opens one initial connection
and grows on demand. Across hosts, budget the sum of both pool maxima plus other
clients below PgBouncer's client limit. For example, six hosts at these defaults
budget 432 clients, leaving 68 of a 500-client pooler for other consumers; adding
hosts or per-process clients requires revisiting that allocation. This arithmetic
is a connection budget, not a throughput benchmark. With direct PostgreSQL URLs,
the same clients consume server connections instead of pooled capacity.

The control pool reserves client connections only: both pools use the same role
and share PgBouncer's backend capacity. Transaction pooling releases a backend
between transactions; a client connection is not a dedicated PostgreSQL backend.
Provider limits such as `AVA_LLM_MAX_CONCURRENT` do not scale with the admission
limit and must be allocated separately across processes and hosts.

The graph is the eight-node self-loop `after_init -> init_context -> claim ->
before_llm -> llm -> before_exec -> exec -> after_exec`. Claim returns to init
context for compaction and to END for idle or lifecycle control. Routing uses
`Command(goto=...)`; plugins extend the hook containers.

## Responsibilities

- `services/agent_host/dispatcher.py` multiplexes Redis wake events and preserves
  single-flight per agent; durable pending work
  supplies the backstop when a pub/sub event is missed.
- `services/agent_host/host.py` admits the exact runtime incarnation, binds
  per-agent context/config, reuses model state, drives graph invocations, flushes
  checkpoints and settles lifecycle state before releasing the turn.
- `agent/hosted_ownership.py` can replace a local owner before its lease expires
  only when the same locked row proves its exact host process has exited and
  its managed resource set is empty and unfrozen. A living host, another machine,
  unknown process identity or unclosed resources retain the admission fences.
  A legacy NULL-resource row has no stored process to prove: it is admitted
  early only through the evidence-gated proposal (renewal silence ≥
  `LEGACY_HOST_ADOPTION_SILENCE_S`, no live same-home agent-host daemon, no
  live exec child of the agent — `shared/host_process_evidence.py`), re-pinned
  to the exact row state under the row lock and recorded as a
  `hosted_legacy_adoption` audit event. NULL evidence alone never authorizes
  takeover.
- `services/agent_host/db_recovery.py` retains the original turn during a database
  or checkpoint failure. A 5s exact-owner probe precedes repair stages with
  independent 30s budgets: flush retained checkpoint writes, reconcile claimed
  inputs, revalidate ownership, repair tool state, and validate ownership again. Retry logs identify
  the failed phase, exception type, SQLSTATE and elapsed time. This repairs the
  agent's checkpoint/inbound consistency; it does not mean PostgreSQL crashed.
- `services/agent_host/recovery_interrupt.py` checks external cancel/terminate
  intent during backoff, advancing one retry without claiming the command or
  interrupting a write. At most one optional query runs per control pool; other
  observers skip that check, preserving capacity for ownership and lifecycle.
  Queries spend the existing backoff budget and create no background tasks.
  Persistent checkpoint unavailability still prevents a completed durable pause.
- `agent/graph/_llm.py` streams model inference with retry and cancellation.
- `agent/graph/_exec.py` runs `execute_code` in a disposable subprocess with an
  owned POSIX process group or Windows Job Object. Cleanup reaps its child and
  joins the output reader; this isolation is independent of host scheduling.
- Persistent shell sessions run in their own PTY hosts and survive normal agent
  restart and cluster pause. Full cluster stop closes them.

## Related contracts

- [[graph.ava.okf.md]] — graph topology and hooks
- [[state.ava.okf.md]] — state and checkpoints
- [[loop.ava.okf.md]] — turn scheduling
- [[lifecycle.ava.okf.md]] — native restart, terminate and resurrection
- [[context.ava.okf.md]] — per-turn dependency injection
- [[tool-calls.ava.okf.md]] — isolated code execution

Agents are allocated through `POST /api/agents`; the gateway commits their row
and work, then the home runner wakes its agent host. No agent Python process is
started directly.
