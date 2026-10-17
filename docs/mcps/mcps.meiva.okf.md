---
type: doc
title: MCP Integrations
description: MCP integrations — agents call external tool servers via `meiva.mcps.<server>.<tool>(...)` (built-in chrome ships with the repo, others install via `meiva mcp install`) over newline-delimited JSON + Unix socket to a per-agent daemon.
tags:
- extensions
- tool
---

# MCP Integrations

This node covers Meiva's agents as MCP clients. The inbound direction (external
clients driving the cluster) is the gateway `/mcp` endpoint:
[[gateway/mcp_server/docs/mcp-endpoint/mcp-endpoint.meiva.okf.md]].

## What It Is
MCP (Model Context Protocol) integrations let agents call external tool servers. Agents invoke tools as `meiva.mcps.<server>.<tool>(...)` — calls serialize into **custom newline-delimited JSON** (`{id,method,params}` → `{id,ok,result|error}`, `meiva/mcps/_daemon.py`) over a Unix socket to the **shared per-machine MCP daemon** (ops roster session "mcp-daemon", watchdog-managed) managing connections to each server. **Standard JSON-RPC is only used for the daemon↔MCP server hop** — over stdio for local servers (`command`) or Streamable HTTP for remote ones (`url`).

## Core Responsibilities
- **Tool discovery**: config **lazy reloaded** on every call (`_load_config`); tool lists lazy-loaded on first access (daemon → 24h disk cache for metadata only), `__all_for_meiva__` prioritizes cache so `help()` triggers no connections — **not a one-time startup load**
- **Tool invocation**: `meiva.mcps.<server>.<tool>(**kwargs)` — sync wrapper over async Unix-socket communication
- **Safety management**: only `_call_text` joins returned text and passes through `scan_content` (`meiva/mcps/_tools.py:call_text`, defined in `meiva/security.py:scan_content`); **arguments are never scanned**, nor are `raw` / `_call_raw` images/structuredContent
- **Daemon required**: missing or unreachable daemon fails execution. Tool discovery may use cached metadata; the SDK never starts a local MCP server.

## Architecture
```
agent process ←─Unix socket─→ shared MCP daemon (per machine) ←─stdio─→ local MCP server (child process)
                                              ├─Streamable HTTP─→ remote MCP server (url endpoint)
                                              └─Unix socket─→ browser-mcp service (chrome)
```
- **One daemon per machine** (not per agent): sessions are isolated **per client connection** — each agent's connection owns its session cache and its MCP server children; connection close releases them
- **Server subprocess sharing**: a server's `.mcp.json` may declare `"shared"` to avoid one stdio child per agent — `"shared": "browser"` (chrome) dials the browser-mcp service's line protocol directly (no child at all, ~63MB saved per agent); `"shared": true` (x) keeps one daemon-wide stdio child for every connection, serialized per server. Remote (`url`) servers spawn no child by construction — the endpoint is dialed per connection
- Tool schema docstrings render in `meiva.help(meiva.mcps.<server>)`

## Configuration Sources (four layers merged, later overrides earlier)
The four `.mcp.json` layers, `~/.meiva/mcp_enabled.json` enable control, `requires` preconditions, and remote-server auth (headers / OAuth 2.1) are documented in [[docs/mcps/configuration.meiva.okf.md]]; the flow itself (discovery → browser → callback → token storage) in [[docs/mcps/oauth.meiva.okf.md]].

## Installation & Startup Form
Native vs installed (mirroring skills), the relative-path `.mcp.json` startup form, per-layer `server_cwd`, and why not `uv run`: [[docs/mcps/installation-startup.meiva.okf.md]].

## Key Dependencies
- [[gateway/mcp_server/docs/mcp-endpoint/mcp-endpoint.meiva.okf.md]] — the inbound direction: this cluster AS an MCP server (gateway `/mcp`)
- [[mcp-daemon.meiva.okf.md]] — MCP daemon subprocess management
- [[agent/docs/state.meiva.okf.md]] — agent identity for socket path

## Entry Points
- `meiva/mcps/__init__.py` — agent-facing tool invocation interface
- `meiva/mcps/config.py` — config loading, four-layer merging, `installed_mcp_dir`
- `meiva/mcps/_oauth.py` — OAuth 2.1 authorization-code + PKCE client builder (browser flow, loopback callback, per-server token storage)
- `meiva/mcps/_daemon.py` — **shared daemon process main loop** (`python -m meiva.mcps._daemon`): binds the shared Unix socket (`$MEIVA_HOME/run/mcp_daemon.sock`) and manages every MCP server's session for every agent connection (per-connection isolation + `"shared"` server buckets)
- `meiva/mcps/_browser.py` — in-daemon line-protocol client for the browser-mcp service (the `"shared": "browser"` chrome path; process-less replacement for `services.desktop.browser.mcp_wrapper`)
- `agent/mcp_daemon.py` — **no-op daemon handle** kept for boot-path compatibility (the daemon is now a supervised cluster service, never a per-agent child)
- `cli/commands/extensions/mcp.py` — `meiva mcp install/uninstall/upgrade/ls/add/remove/enable/disable`
- `cli/commands/extensions/_pkg_source.py` — install source fetching (git URL / local path), shared with `meiva plugins install`
- `base/packages/extensions/install_registry.py` — install registry (`type="mcp"` rows = installed MCPs)
- `base/packages/plugins/mcp_enabled.py` — enable/disable configuration management
- `meiva_builtins/mcps/chrome/.mcp.json` — chrome server definition (`"shared": "browser"` — the daemon dials the browser-mcp service directly; the `services.desktop.browser.mcp_wrapper` stdio bridge is retained only as the declared command for hosts running older daemons); scanned by `meiva/mcps/config.py:builtin_mcp_paths()`

## Current MCP Servers
| Server | Type | Purpose | Expand |
|--------|------|------|------|
| chrome | native (built-in) | Drive a logged-in Chrome browser: navigation, click, form fill, screenshot, DOM/console reading | [[chrome.meiva.okf.md]] |
| x | installed (`meiva mcp install <repo-url> --env X_BEARER_TOKEN=<token>`) | X (Twitter) search/post/timeline — bridge to X's hosted MCP, tool set determined by X | — |

## Notes
- MCP is the only mechanism for agents to reach external processes — all other capabilities run in the `execute_code` sandbox
- Daemon crash does not kill the agent — next MCP call raises `MCPConnectError`
- Tool schemas cached on disk 24h, reducing startup round-trips
- The official `mcp` SDK is pinned `>=2.0.0,<3` (2026-07-28 protocol revision): SDK field names are snake_case (`input_schema` / `is_error` / `structured_content`); the daemon serializes content with `by_alias=True`, so the agent-facing wire protocol stays camelCase (unchanged from pre-v2)
