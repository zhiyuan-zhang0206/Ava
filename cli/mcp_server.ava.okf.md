---
type: doc
title: ava mcp serve — Ava as an MCP Server
description: '`ava mcp serve` (`cli/mcp_server.py`) — the inbound MCP direction: a stdio server whose seven tools are this cluster''s gateway control routes, so an external agent (Claude Code, Codex) drives the fleet. A thin proxy with no control logic and no state. Deprecated in favor of the gateway /mcp Streamable HTTP endpoint (design task #1212); behavior unchanged until retirement.'
tags:
- cli
- tool
- mcp
---

# `ava mcp serve` — Ava as an MCP Server

## What it is

The inverse of the rest of [[commands/packages.ava.okf.md|the `ava mcp` family]].
Those verbs configure servers Ava's own agents call **out** to; `serve` runs an
MCP server over stdio whose tools are this cluster's own control plane, so an
external agent calls **in** and drives the fleet:

```bash
claude mcp add ava -- ava mcp serve       # codex mcp add takes the same form
```

## Tool surface

One gateway route each — seven, deliberately: enough to run a fleet, small
enough that an external model can hold the whole surface.

| Tool | Route | Effect |
|---|---|---|
| `spawn_agent` | `POST /api/agents` | start an agent on a goal; returns its id at once |
| `send_message` | `POST /api/agents/{id}/messages` | queue an instruction / answer for a running agent |
| `list_agents` | `GET /api/agents?fields=summary` | every agent, compacted to the fields a caller steers by |
| `get_agent` | `GET /api/agents/{id}` | one agent's full state, incl. blocking questions |
| `get_messages` | `GET /api/agents/{id}/messages` | transcript as role + text + the code it ran |
| `terminate_agent` | `POST /api/agents/{id}/terminate` | destructive: end the agent; `message` is retained for resurrection (`force` kills mid-step) |
| `cluster_status` | `GET /api/cluster/status` | is the cluster up, and is it paused |

`list_agents` and `get_messages` project rather than forward: the raw rows carry
pids, activity timestamps and provider metadata that cost a model context
without changing any decision. `get_messages` keeps the `execute_code` argument
of each turn — Ava agents act by writing Python, so dropping the code would show
an agent that talks and never acts.

## Invariants

- **No control logic, no state.** Every tool is an authenticated gateway call
  the web UI already makes, so an MCP client can do exactly what a browser can
  and nothing more. New capability belongs on a gateway route first.
- **Identity is not a parameter.** `shared.machine.gateway_api_base` +
  `gateway_auth_headers` resolve the gateway and cluster secret of the checkout
  the running `ava` belongs to — the same checkout-anchored rule as every CLI
  verb, so `ava` on PATH serves prod and a worktree's `.venv/bin/ava` serves that
  worktree's cluster. There is no cluster argument to get wrong.
- **stdout is the wire.** Under stdio transport a stray `print()` corrupts the
  JSON-RPC stream. The mcp SDK logs through a stderr `RichHandler` and
  `shared.log` adds only stderr sinks, which is what makes this path safe.
- **Provenance is explicit.** Spawns carry `spawner="mcp"`, so fleet views group
  externally created agents on their own root; prompts and messages carry
  envelope source `user`, since an MCP client acts for the human driving it.
- **Errors are forwarded, never softened.** A gateway failure becomes a
  `ToolError` carrying the gateway's own `detail` (a 422's per-field list
  flattened to `loc: msg`). That message is the only channel the external model
  has for correcting its own call, and a 404 that read as an empty result would
  be indistinguishable from an idle agent. The one filter applied proxy-side —
  `list_agents(status=...)` — validates against `AgentStatus` for the same
  reason: an unrecognized value errors with the legal set rather than returning
  the empty list a model would read as "the fleet is empty".

## Notes

- Built on the official `mcp` SDK's `MCPServer` (the v2 rename of `FastMCP`, speaking the 2026-07-28 protocol revision) — tool schemas come from the
  function signatures, so the advertised arguments cannot drift from the code.
- `build_server()` is a builder, not a module singleton: nothing registers as an
  import side effect, and a test can list and call tools with no live gateway.
- The verb routes through `cli/mcp_server.py` rather than a `cli/commands/`
  module because it is a long-running server, not a command that renders and
  exits, and it is the only verb needing the mcp SDK import.

## Key Dependencies

- [[okf/mcps/mcps.ava.okf.md|MCP integrations]] — the domain node; the outbound half
- [[commands/packages.ava.okf.md]] — the rest of the `ava mcp` verb surface
- [[gateway/routers/ops-surfaces.ava.okf.md|gateway routes]] — the control plane being proxied
