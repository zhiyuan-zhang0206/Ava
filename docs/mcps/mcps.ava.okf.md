---
type: doc
title: MCP Integrations
description: MCP integrations — agents call external tool servers via `ava.mcps.<server>.<tool>(...)` (built-in chrome ships with the repo, others install via `ava mcp install`) over newline-delimited JSON + Unix socket to a per-agent daemon.
tags:
- extensions
- tool
---

# MCP Integrations

This node covers Ava's agents as MCP clients. The inbound direction (external
clients driving the cluster) is the gateway `/mcp` endpoint:
[[gateway/mcp_server/docs/mcp-endpoint.ava.okf.md]].

## What It Is
MCP (Model Context Protocol) integrations let agents call external tool servers. Agents invoke tools as `ava.mcps.<server>.<tool>(...)` — calls serialize into **custom newline-delimited JSON** (`{id,method,params}` → `{id,ok,result|error}`, `ava/mcps/_daemon.py`) over a Unix socket to the **shared per-machine MCP daemon** (ops roster session "mcp-daemon", watchdog-managed) managing connections to each server. **Standard JSON-RPC is only used for the daemon↔MCP server hop** — over stdio for local servers (`command`) or Streamable HTTP for remote ones (`url`).

## Core Responsibilities
- **Tool discovery**: config **lazy reloaded** on every call (`_load_config`); tool lists lazy-loaded on first access (daemon → 24h disk cache → local connection), `__all_for_ava__` prioritizes cache so `help()` triggers no connections — **not a one-time startup load**
- **Tool invocation**: `ava.mcps.<server>.<tool>(**kwargs)` — sync wrapper over async Unix-socket communication
- **Safety management**: only `_call_text` joins returned text and passes through `scan_content` (`ava/mcps/__init__.py:_call_text`, defined in `ava/security.py:scan_content`); **arguments are never scanned**, nor are `raw` / `_call_raw` images/structuredContent
- **Graceful degradation**: daemon path wrapped in `suppress(MCPConnectError, OSError)` → **silently falls back to local stdio** (agent spawns the server itself); raises `MCPConnectError` only if local also fails

## Architecture
```
agent process ←─Unix socket─→ shared MCP daemon (per machine) ←─stdio─→ local MCP server (child process)
                                              ├─Streamable HTTP─→ remote MCP server (url endpoint)
                                              └─Unix socket─→ browser-mcp service (chrome)
```
- **One daemon per machine** (not per agent): sessions are isolated **per client connection** — each agent's connection owns its session cache and its MCP server children; connection close releases them
- **Server subprocess sharing**: a server's `.mcp.json` may declare `"shared"` to avoid one stdio child per agent — `"shared": "browser"` (chrome) dials the browser-mcp service's line protocol directly (no child at all, ~63MB saved per agent); `"shared": true` (x) keeps one daemon-wide stdio child for every connection, serialized per server. Remote (`url`) servers spawn no child by construction — the endpoint is dialed per connection
- Tool schema docstrings render in `ava.help(ava.mcps.<server>)`

## Configuration Sources (four layers merged, later overrides earlier)
The four `.mcp.json` layers, `~/.ava/mcp_enabled.json` enable control, `requires` preconditions, and remote-server auth (headers / OAuth 2.1) are documented in [[docs/mcps/configuration.ava.okf.md]]; the flow itself (discovery → browser → callback → token storage) in [[docs/mcps/oauth.ava.okf.md]].

## Installation & Startup Form
Native vs installed (mirroring skills), the relative-path `.mcp.json` startup form, per-layer `server_cwd`, and why not `uv run`: [[docs/mcps/installation-startup.ava.okf.md]].

## Key Dependencies
- [[gateway/mcp_server/docs/mcp-endpoint.ava.okf.md]] — the inbound direction: this cluster AS an MCP server (gateway `/mcp`)
- [[mcp-daemon.ava.okf.md]] — MCP daemon subprocess management
- [[agent/docs/state.ava.okf.md]] — agent identity for socket path

## Entry Points
- `ava/mcps/__init__.py` — agent-facing tool invocation interface
- `ava/mcp_config.py` — config loading, four-layer merging, `installed_mcp_dir`
- `ava/mcps/_oauth.py` — OAuth 2.1 authorization-code + PKCE client builder (browser flow, loopback callback, per-server token storage)
- `ava/mcps/_daemon.py` — **shared daemon process main loop** (`python -m ava.mcps._daemon`): binds the shared Unix socket (`$AVA_HOME/run/mcp_daemon.sock`) and manages every MCP server's session for every agent connection (per-connection isolation + `"shared"` server buckets)
- `ava/mcps/_browser.py` — in-daemon line-protocol client for the browser-mcp service (the `"shared": "browser"` chrome path; process-less replacement for `services.desktop.browser.mcp_wrapper`)
- `agent/mcp_daemon.py` — **no-op daemon handle** kept for boot-path compatibility (the daemon is now a supervised cluster service, never a per-agent child)
- `cli/commands/extensions/mcp.py` — `ava mcp install/uninstall/upgrade/ls/add/remove/enable/disable`
- `cli/commands/extensions/_pkg_source.py` — install source fetching (git URL / local path), shared with `ava plugins install`
- `base/packages/extensions/install_registry.py` — install registry (`type="mcp"` rows = installed MCPs)
- `base/packages/plugins/mcp_enabled.py` — enable/disable configuration management
- `ava_builtins/mcps/chrome/.mcp.json` — chrome server definition (`"shared": "browser"` — the daemon dials the browser-mcp service directly; the `services.desktop.browser.mcp_wrapper` stdio bridge is retained only as the declared command for hosts running older daemons); scanned by `ava/mcp_config.py:builtin_mcp_paths()`

## Current MCP Servers
| Server | Type | Purpose | Expand |
|--------|------|------|------|
| chrome | native (built-in) | Drive a logged-in Chrome browser: navigation, click, form fill, screenshot, DOM/console reading | [[chrome.ava.okf.md]] |
| x | installed (`ava mcp install <repo-url> --env X_BEARER_TOKEN=<token>`) | X (Twitter) search/post/timeline — bridge to X's hosted MCP, tool set determined by X | — |

## Notes
- MCP is the only mechanism for agents to reach external processes — all other capabilities run in the `execute_code` sandbox
- Daemon crash does not kill the agent — next MCP call raises `MCPConnectError`
- Tool schemas cached on disk 24h, reducing startup round-trips
- The official `mcp` SDK is pinned `>=2.0.0,<3` (2026-07-28 protocol revision): SDK field names are snake_case (`input_schema` / `is_error` / `structured_content`); daemon and local fallback re-serialize with `by_alias=True`, so the agent-facing wire protocol stays camelCase (unchanged from pre-v2)
