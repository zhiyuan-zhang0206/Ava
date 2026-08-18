---
type: doc
title: Agent Main Loop
description: Thin launcher for Ava agent processes. Each OS process is permanently bound to an `agent_id`, via `python -m agent --agent-id
tags: []
---

# Agent Main Loop

## What it is

Thin launcher for Ava agent processes. Each OS process is permanently bound to an `agent_id`, started via `.venv/bin/python -m agent --agent-id N`. After startup, it enters the LangGraph graph loop and never returns — until it receives a `terminate` inbound message and exits normally.

## Core Responsibilities

- **Process entry point**: `main()` parses `--agent-id`, initializes DB/Redis/LLM/Saver, starts infinite loop with `graph.ainvoke()`
- **State transitions**: `allocated → starting → running` (`_starting.py` declares `starting` before heavy imports, claim_node transitions to `running` on first run)
- **Inbound messages**: waits for messages via Redis pub/sub channel, `await listener.wait_one()` in claim_node
- **Normal exit**: receives terminate inbound → claim_node goto END → `ainvoke` returns → `_notify_exit()` → process exits
- **Lifecycle signals**: SIGHUP/SIGTERM → SystemExit → `_notify_exit()` in finally block
- **DB-outage pause** (laptop sleep/network change/tailscale blackhole): `main()`'s `while True` catches `PoolTimeout` / `psycopg.OperationalError` and **does not exit** — backoff + probe until DB recovers, re-runs the same startup reconciliation as a fresh process in-process (two-phase claimed-inbound repair + dangling tool_use repair), then re-enters `ainvoke` graph. Single branch handles DB disconnection for idle-recheck / mid-turn / LLM envelope exhaustion; process survives throughout, reaper won't kill by mistake, gateway view unchanged (row remains running/idling)

- **Liveness lease renewal**: a background task (`_renew_agent_lease_loop`) re-arms `agents_meta.lease_expires_at` every `AGENT_LEASE_RENEW_INTERVAL_S` (60 s) for the whole graph lifetime, idle included — the lease is what quiesce/reaper/frontend read as "this process is alive". See [[lease.ava.okf.md|Agent Liveness Lease]].
- **Watcher reconcile at boot**: before the graph goes live, rebuilds watcher sessions a stop/rollout reaped from the `agent_watchers` registry (cron re-spawned from its stored expression; missed one-shots marked + alerted) — best-effort, a registry failure never blocks boot. See [[shared/watcher_registry.ava.okf.md|Watcher Registry]].

## Key Dependencies

- [[graph.ava.okf.md]] — LangGraph execution graph
- [[state.ava.okf.md]] — state channels
- [[lifecycle.ava.okf.md]] — process exit handling
- [[db.ava.okf.md]] — Postgres connection pool
- [[mcp-daemon.ava.okf.md]] — MCP subprocess management

## Entry Points

- `agent/loop.py:main()` — main function
- `agent/loop.py:_renew_agent_lease_loop` — the lease renewal task
- `agent/__main__.py` — `.venv/bin/python -m agent`
- `agent/_starting.py` — early startup (state declaration + row claim)

## Notes

- New processes are never started directly — always go through the gateway's `POST /api/agents` HTTP path
- The agent's shell sessions are **not cleaned** on exit; background work persists across lifecycles
- The `ava` module is imported once at the process level, worker threads share via `sys.modules` cache
