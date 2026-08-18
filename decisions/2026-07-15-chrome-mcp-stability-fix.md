# Chrome MCP Frequent Disconnection Diagnosis and Fix

## Background

Multiple agents reported frequent Chrome MCP disconnections, with the error chain being:
1. `chrome upstream session is down; browser-mcp will restart`
2. `chrome MCP daemon not reachable at chrome-mcp.9222.sock: No such file or directory`

Agent 1750 was the most severely affected, and agent 1447 accumulated 60 disconnections.

## Architecture

```
Agent (execute_code)
  └─> ava.mcps.chrome.* (in-process)
       └─> Unix socket → per-agent MCP daemon (ava._mcps_daemon.py)
            └─> stdio → per-agent chrome wrapper (services.browser.mcp_wrapper.py)
                 └─> Unix socket → shared chrome MCP daemon (services.browser.mcp_daemon.py)
                      └─> stdio → chrome-devtools-mcp (npx package)
                           └─> CDP → Google Chrome
```

## Diagnostic Findings

### Root Cause #1: chrome-devtools-mcp Memory Leak (Trigger)

- PID 88023 had an RSS of 3.2GB (19.3% of the system's 16GB), and it continued to grow
- chrome-devtools-mcp collects network/console traffic for each tab, growing indefinitely over time
- Memory pressure caused the npx process to be killed by OOM or crash internally

### Root Cause #2: browser MCP daemon Exits After Disconnection (Amplifies Failure)

- `services/browser/mcp_daemon.py:run()` sets `daemon.dead` and exits upon upstream disconnection
- After the socket disappears, the healthcheck detects it every 60s, taking up to 60s to respawn
- Within this 60s window, all agent Chrome calls fail

### Root Cause #3: Insufficient Retries in mcp_wrapper

- `_ReconnectingLink._is_transport_error()` does not recognize `RuntimeError` from upstream disconnection
- Retry parameters: 3 attempts, 0.5s base delay → max 3.5s, far from enough to cover daemon restart

## Fix

### PR #457: fix(browser-mcp): auto-reconnect upstream on death instead of exiting

**mcp_daemon.py** — Core fix:
- Extracted `_create_upstream()` function
- Changed `run()` to a reconnection loop: upstream death → close old stack → exponential backoff reconnect (1s→30s)
- `daemon_ref` pattern: `_handle_client` always sees the current daemon via a mutable reference
- `ping` still succeeds during reconnection, so the healthcheck won't mistakenly kill it

**mcp_wrapper.py** — Improvements:
- `_ReconnectingLink` now recognizes upstream disconnection `RuntimeError` as a transport error
- Retry attempts increased from 3 to 5, base delay from 0.5s to 1.0s

### To Follow Up

The chrome-devtools-mcp memory leak itself is not fixed (external npx package). As a subsequent optimization, we could consider periodically restarting the upstream (e.g., every N hours or proactively restarting when memory exceeds a threshold).
