---
type: doc
title: Page Restore
description: How open serve()/show() pages are probed and restored — runtime construction, heartbeat, and host scans; per-agent throttle and turn-identity binding.
tags: []
---

# Page Restore

## What it is

Open `serve()`/`show()` pages are rows in `agent_pages`; the page-server daemon supervises serve() servers inside agent-owned persistent shell sessions, which are outside rollout service teardown. When a page server dies (platform update reaping the session, crash, OOM, manual kill) the row stays open and the link goes dead — recovery is the agent-side probe `agent/startup.py:reconcile_open_pages()`, with dead-page close and notification writes in `agent/_page_reconcile.py`.

Per open row:

- server alive -> keep (log only)
- server dead + `serve_dir` set (serve()) -> re-serve the recorded directory via `ava.ui.serve`; the old link works again
- server dead + no `serve_dir` (show() pages / pre-serve_dir rows) -> cannot be rebuilt: close the row (CAS `closed_at`, same UPDATE close_page uses) and tell the agent to re-serve with one system inbound, deduped per 6h

## Trigger points

- **Heartbeat** (`agent/graph/_claim_dispatch.py:_handle_heartbeat`) — every check-in of an IDLE agent (~5 min)
- **Periodic** (`agent/startup.py:page_reconcile_loop`) — every heartbeat interval regardless of idle/busy: busy agents get no heartbeats, so without this a busy agent's pages stay dead for as long as its turn lasts (2026-09-01 incident: ~4h). The boot scan covers t=0; the loop covers everything after.
- **Hosted daemon** (`services/agent_host/daemon.py:_page_reconcile_forever`) — the daemon scans every heartbeat interval with the first pass immediately at start (`reconcile_all_open_pages`). Each per-agent pass runs under `bind_turn_identity`: the daemon process has no agent identity, and the re-serve arm reads `ava._boot.agent_id()` — without the bind the registration POST would target /agents/None and pages would never heal.

## Throttle

One pass per agent per interval: `agent/startup.py:_last_reconcile_at`, a monotonic timestamp keyed by agent_id — the hosted daemon serves many agents in one process, so a single shared value would let one agent's heartbeat scan suppress every other agent's pass. A pass stamps its agent's key; the periodic loops skip an agent whose key is fresh. Plain float values, no asyncio.Lock (which would bind to one event loop); the no-lock argument assumes a single event loop per process.

## Failure handling

Best-effort everywhere: query / probe / serve failures are logged and swallowed — the page heals on the next pass. The periodic loops are self-protecting (any raise is logged, the next interval retries). PageClosed events for closed rows go through the caller's event publisher; the hosted daemon publishes best-effort on the shared Redis channel (gateway ttl_reaper pattern).
