---
type: doc
title: Page Restore
description: How open serve()/show() pages are probed and restored — agent boot and heartbeat, and the page-server service's scan of dead show() pages.
tags: []
---

# Page Restore

## What it is

Open `serve()`/`show()` pages are rows in `agent_pages`; the page-server daemon supervises serve() servers inside agent-owned persistent shell sessions, which survive agent restarts but not `ava stop` or `ava restart`. When a page server dies (platform update reaping the session, crash, OOM, manual kill) the row stays open and the link goes dead — recovery is the agent-side probe `agent/startup/__init__.py:reconcile_open_pages()`, with dead-page close and notification writes in `agent/startup/_page_reconcile.py`.

Per open row:

- server alive -> keep (log only)
- server dead + `serve_dir` set (serve()) -> re-serve the recorded directory via `ava.ui.serve`; the old link works again
- server dead + no `serve_dir` (show() pages / pre-serve_dir rows) -> cannot be rebuilt: close the row (CAS `closed_at`, same UPDATE close_page uses) and tell the agent to re-serve with one system inbound, deduped per 6h

## Trigger points

- **Boot** — the agent's own scan covers t=0.
- **Heartbeat** (`agent/graph/claim/_dispatch.py:_handle_heartbeat`) — every check-in of an IDLE agent (~5 min).
- **Page-server service** (`services/agent_runner/page_server/dead_pages.py`) — a busy agent gets no heartbeats, so without a scan its dead pages stay dead for as long as its turn lasts (2026-09-01 incident: ~4h). The page-server's `dead_show_pages` loop scans every open show() page of the machine every heartbeat interval (first pass at start) and runs the close-and-notify arm above in one transaction through the same notice, dedupe window and statements (`base/agents/recovery/pages.py`). A dead `serve()` page needs no scan there: the daemon's own reconcile relaunches its server within one poll.

## Failure handling

The agent-side pass is best-effort: query / probe / serve failures are logged and swallowed — the page heals on the next pass. The service loop skips a round on an unreachable database and ends the process on any other exception. PageClosed events for rows closed on the agent side go through the caller's event publisher.
