---
type: doc
title: MCP Daemon
description: MCP (Model Context Protocol) server manager — ONE per-machine shared daemon process serving every agent via Unix socket, with per-connection session isolation. Implemented by `ava/_mcps_daemon.py`, run as ops roster service "mcp-daemon" (watchdog-managed).
tags: []
---

# MCP Daemon

## What it is
MCP (Model Context Protocol) server manager. A single per-machine daemon process (`python -m ava._mcps_daemon`, ops roster session **mcp-daemon**, watchdog-managed like browser-mcp) owns the lifecycle of all MCP server sessions on the host and serves every agent over one Unix socket (`$AVA_HOME/run/mcp_daemon.sock`).

This replaced the previous design where each agent spawned its own ~12MB daemon child (15 agents = 15 processes). Sharing the process shares no state: **session isolation is per client connection** — each agent's connection gets its own session cache, and when a connection closes its MCP server subprocesses are released.

## Core Responsibilities
- **One shared daemon per machine**: binds `mcp_daemon_shared_socket()` (`$AVA_HOME/run/mcp_daemon.sock`); no agent_id in the path
- **Per-connection session isolation**: sessions/stacks/locks are created inside `_handle_connection`; agent A can never reach agent B's chrome/x sessions; connection close cleans up its MCP server children
- **Server subprocess sharing** (`"shared"` spec, `ava/_mcps_daemon.py`): a `.mcp.json` entry may declare `"shared": "browser"` (dial the browser-mcp service's line protocol directly — no stdio child at all, replacing the ~63MB per-agent `mcp_wrapper`) or `"shared": true` (one daemon-wide stdio child serves every connection, serialized per server, for stateless servers like x). Only `"shared": true` sessions live in the daemon-wide buckets (`shared_sessions`/`shared_stacks`/`shared_locks`), released at daemon shutdown, not on connection close; `"shared": "browser"` sessions are per-connection — each agent connection keeps its own socket, so the browser-mcp service's page affinity isolates agents and no connection can corrupt another's request-id stream
- **Remote (`url`) servers**: `_connect_server` routes a `url` entry to `_connect_http` — Streamable HTTP (`streamable_http_client`), no child process. Static auth rides in `headers`; `"oauth": true` builds the OAuth 2.1 client (`_mcp_oauth.py`, browser flow) and stretches the connect envelope to 600 s (`_OAUTH_FLOW_TIMEOUT_S`); failures close the local stack and fail fast, same discipline as stdio
- **JSON-line protocol over Unix socket**: `{"id", "method": "list_tools"|"call_tool"|"ping", "params": {...}}` — `ping` is the lock-free liveness probe the watchdog healthcheck uses — result payloads keep camelCase (`inputSchema` / `isError` / `structuredContent`): SDK v2 field names are snake_case, re-serialized `by_alias=True`, so this agent-facing contract never changed with the SDK upgrade
- **Transport-error retry**: dead MCP server subprocess → invalidate session, back off (1/2/4s), reconnect (shared servers rebuild the daemon-wide child the same way); transport detection also matches the v2 `MCPError` name
- **Graceful degradation**: agent-side `ava.mcps` falls back to local mode when the shared socket is absent

## Key Dependencies
- `ops/spec.py` — ServiceSpec "mcp-daemon" (agent-runner capability, `requires_db=False`, healthcheck `services.healthchecks.mcp_daemon`)
- [[sdk-surface.ava.okf.md]] — `ava.mcps` SDK connects to the shared socket
- [[oauth.ava.okf.md]] — OAuth 2.1 authorization-code + PKCE flow for remote servers

## Entry Points
- `ava/_mcps_daemon.py:main()` — daemon entry; no args = shared socket, one arg = explicit path (tests)
- `ava/_mcps_daemon.py:run_daemon(socket_path)` — bind + serve; per-connection `_handle_connection`
- `ava/_mcp_browser.py:connect_browser_direct()` — in-daemon line-protocol client for the browser-mcp service (the `"shared": "browser"` path; no subprocess)
- `ava/_mcps_daemon.py:_connect_http()` — remote Streamable HTTP / OAuth connect (no child process)
- `services/healthchecks/mcp_daemon.py` — 60s watchdog probe (ping) + `respawn_service` restart

## Notes
- Session cache lives per connection for non-shared servers: two agents listing the same server each spawn their own MCP server child (isolation for stateful servers). `"shared"` servers opt into one child for everyone: `"browser"` (chrome — process-less direct dial to the browser-mcp service, keeping per-connection page affinity) or `true` (x — one daemon-wide stdio child, serialized; safe because that server keeps no per-connection state)
- A shared stdio child stays resident after its last connection closes (released at daemon shutdown) — the memory win is N×child → 1×child, not child → 0
- `mcp_daemon_shared_socket()` lives in `shared/paths.py` — the single source of truth for the naming convention
- Old per-agent socket files (`mcp_daemon.<id>.sock`) are obsolete; a leftover stale file is harmless (nothing binds it)
