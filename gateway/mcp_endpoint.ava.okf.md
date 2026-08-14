---
type: doc
title: Gateway /mcp endpoint — control plane as an MCP server
description: '`gateway/mcp_endpoint.py` — the cluster control plane exposed over Streamable HTTP at `/mcp` on the gateway (design task #1212 step 1). Seven tools (list_agents / get_agent / spawn_agent / send_message / get_messages / terminate_agent / cluster_status) are thin handlers over the same internal functions the REST routers call; stateless transport (2026-07-28 revision), cluster-middleware auth, every tools/call audited as a `mcp_tool_call` event. Flag-gated off by default (AVA_MCP_ENDPOINT_ENABLED).'
tags:
- gateway
- mcp
- tool
---

# Gateway `/mcp` endpoint

## What it is

The first step of the MCP-over-gateway design (task #1212): one Streamable HTTP
MCP endpoint on the gateway that external MCP clients (Claude Code / Codex /
anything) dial to drive the fleet — the same control effects the web UI and the
`ava` CLI reach over the REST API. Later steps add the machine-routing layer
(per-machine tool servers, client_key identity, stash+chunk large payloads);
this step is control plane only.

The seven tools are **thin handlers over the same internal functions the REST
routers call** (`_spawn_preflight_blocking` + `_forward_spawn_to_remote`,
`post_agent_terminate`, `deliver_chat_inbound`, `load_checkpoint_messages`,
`agent_snapshot`, `get_cluster_status`) — no business logic of its own, no
self-HTTP round-trip (2026-06-07 CLI↔gateway boundary decision). The tool
surface and result shapes match the existing stdio `ava mcp serve`, which this
endpoint replaces over time.

## Mechanics

- **Flag-gated, additive**: `settings.gateway.mcp_endpoint_enabled`
  (AVA_MCP_ENDPOINT_ENABLED), default off. Off, `/mcp` answers 404 via the
  `mcp_gateway` ASGI wrapper; the existing mcp-daemon path and `ava mcp serve`
  are untouched either way.
- **Mount**: `app.mount("/mcp", mcp_gateway(app))` — the wrapper reads
  `app.state.mcp_manager`, set by the gateway lifespan, which builds a fresh
  `StreamableHTTPSessionManager` per lifespan entry and enters
  `manager.run()` (it can only run once per instance).
- **Transport**: stateless Streamable HTTP — one fresh transport per POST, no
  server-side session state, no idle reaping. `host=""` skips the SDK's
  loopback-only DNS-rebinding auto-guard (this endpoint is embedded in the
  gateway and dialed at the machine's reachable hostname).
- **Auth**: the cluster middleware (session cookie / Bearer secret) like every
  other route — no bypass entry.
- **Audit**: a `_AuditMiddleware` on the MCPServer records every `tools/call`
  as a `mcp_tool_call` audit event (tool, args, outcome ok/error, error text;
  agent_id NULL — external clients have no agent identity yet).

## Why not a router

`/mcp` is not a FastAPI router: the MCP protocol is JSON-RPC over HTTP with its
own lifecycle (initialize handshake), served by the mcp SDK's ASGI app — so it
mounts as a raw ASGI wrapper rather than joining the `/api/*` router set.
