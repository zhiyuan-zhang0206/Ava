---
type: doc
title: Gateway /mcp endpoint — control plane as an MCP server
description: '`gateway/mcp_endpoint.py` — the cluster control plane exposed over Streamable HTTP at `/mcp` on the gateway. Seven tools are thin handlers over the REST routers'' internal functions; stateless transport, revocable read/write client tokens, and client-identified audit events with redacted arguments. Flag-gated off by default (AVA_MCP_ENDPOINT_ENABLED).'
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
(per-machine tool servers, stash+chunk large payloads); this step is control
plane only.

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
- **Auth**: `/mcp` bypasses the cluster middleware and its ASGI wrapper requires
  `Authorization: Bearer <MCP client token>`. Tokens are generated per client,
  stored only as SHA-256 hashes in `mcp_clients`, and can be revoked through the
  cluster-authenticated `/api/mcp/clients` admin routes. A no-secret cluster
  still requires an MCP client token; cluster cookies and secrets never count.
  Messages written through this boundary record `mcp_client:<id>` as their
  server-verified credential fact without storing the token.
- **Scope**: `read` clients may list/inspect agents, messages, and cluster
  status. `spawn_agent`, `send_message`, and `terminate_agent` require `write`.
- **Roster reads**: `list_agents` starts from the same SQL-level agent summary
  projection as the REST, SDK, and stdio MCP roster reads; `get_agent` remains
  the full single-agent diagnostic view.
- **Audit**: a `_AuditMiddleware` on the MCPServer records every `tools/call`
  as a `mcp_tool_call` event with client id/name and outcome. Each argument is
  represented only by its JSON type, character size, and SHA-256; raw values
  never enter the event. `agent_id` stays NULL for this service-level identity.

## Why not a router

`/mcp` is not a FastAPI router: the MCP protocol is JSON-RPC over HTTP with its
own lifecycle (initialize handshake), served by the mcp SDK's ASGI app — so it
mounts as a raw ASGI wrapper rather than joining the `/api/*` router set.
