---
type: doc
title: MCP Server System
description: Ava's MCP (Model Context Protocol) server integration — how external tools are exposed to agents.
tags:
- core
- agent-instruction
---

# MCP Server System

MCP servers expose external tools to Ava agents at runtime. Each server
registers one or more tools that agents call through `ava.mcps.<server>.<tool>()`.

## Available Servers
- [[ava_builtins/mcps/chrome/chrome.ava.okf.md|Chrome]] — browser automation (navigate, click, fill forms, screenshots)

## How MCP Works
MCP servers run as child processes managed by the shared per-machine MCP
daemon (`ava/_mcps_daemon.py`, ops roster session "mcp-daemon"). Tool
discovery is **lazy, not at startup**: config reloads on every call
(`_load_config`), and tool lists load on first access (daemon → 24h disk
cache → local connection) — `servers()`/`help()` prioritize the cache so
they trigger no connections; the daemon does not prefetch at boot.

Remote `url` entries spawn no child: the daemon dials the Streamable HTTP endpoint directly, and the local fallback (`_connect`) has the same `url` branch when the daemon socket is absent.

**Server subprocess sharing**: a server's `.mcp.json` may declare `"shared"`
to avoid one stdio child per agent connection. `"shared": "browser"`
(chrome) makes the daemon dial the browser-mcp service directly — no child
at all, replacing the ~63MB per-agent wrapper. `"shared": true` (x) keeps one daemon-wide child for every connection, serialized per server
(only safe for stateless servers; stateful servers stay per-connection).

**Remote servers**: a `url` entry (validated by `ava/_mcp_config.py:server_url`) is an http(s) Streamable HTTP endpoint — mutually exclusive with `command` — with one auth mode: static `headers` (str → str, API-key style) or `"oauth": true`, a full OAuth 2.1 authorization-code + PKCE browser flow ([[oauth.ava.okf.md]]).


## See Also
- [[ava/skills.ava.okf.md|Skill System]] — skills vs MCP servers

# ava.mcps — MCP Tool Server

## What it is

`ava.mcps` exposes tools from external MCP (Model Context Protocol) servers. Each MCP server appears as a sub-module `ava.mcps.<server>`, with tools as functions on it.

## Core API

- `ava.mcps.<server>.<tool>(...)` — call an MCP tool. Pass parameters as named arguments (positional not allowed).
- `ava.mcps.<server>.raw(tool, **args) → dict` — get the raw structured return of a tool (default call returns text).
- `servers() → list[str]` — list all configured MCP server names.
- `description(server) → str | None` — get a one-line description of a server.
- `help()` — print summary of all MCP servers.

## Where Servers Come From

The server set is determined by config (not enumerated in `ava/` source code). `ava/_mcp_config.py:load_mcp_config` **four-layer merge** (later overwrites same name):
1. builtin `<repo>/ava_builtins/mcps/*/.mcp.json` (builtin layer, symmetric with builtin skills/plugins);
2. bundled `.mcp.json` under each plugin root (`mcpServers` section);
3. installed `$AVA_HOME/mcps/*/.mcp.json` (installed outside core via `ava mcp install`, gated by `install_registry` rows of `type="mcp"`);
4. machine-level `$AVA_HOME/mcp.json` (applied last, overwrites defaults of same name).
On top of this, per-host **disabled overlay** (`shared/mcp_enabled.py:read_enabled`) — servers marked disabled are excluded from the returned map by default.

Installed server spawn cwd is given by `installed_mcp_dir(name)` (its package directory), allowing its relative `.venv/bin/python` command to resolve to an isolated venv; builtin/plugin/machine returns None (keeping daemon cwd).

Server entries may carry `requires` host-capability pre-checks; when unmet, an actionable capability error is returned rather than an opaque failure from the underlying tool. Two keys are recognized (`ava/_mcp_config.py:assert_requirements`): `display` and `unix_socket`; an unknown key fails fast, so a typo can never silently disable a gate. `chrome` declares both — its wrapper reaches the `browser-mcp` daemon over a Unix socket, so the entry is gated off on Windows exactly where that daemon is. Builtin server currently includes only **chrome** (drives a logged-in browser: navigate/click/fill forms/screenshot/read DOM); other servers are installed outside core via `ava mcp install`; rest come from machine-level `mcp.json`.

## Key Dependencies
- [[mcp-daemon.ava.okf.md]] — MCP subprocess manager (long-lived serial connection process)
- [[oauth.ava.okf.md]] — OAuth 2.1 authorization-code + PKCE flow for remote servers

## Notes
Tool arguments are named — must be passed by name. Errors are returned as `MCPCallError`.

SDK v2 (pinned `>=2.0.0,<3`, 2026-07-28 protocol revision): SDK field names are snake_case (`input_schema` / `is_error` / `structured_content`); `_dump_content` and the daemon re-serialize `by_alias=True`, so the camelCase wire contract agents parse is unchanged.
