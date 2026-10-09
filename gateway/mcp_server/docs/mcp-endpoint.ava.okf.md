---
type: doc
title: Gateway /mcp endpoint — control plane as an MCP server
description: 'Gateway MCP control tools with scoped credentials, retained creation receipts, stateless transport and redacted audit events.'
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
routers call** (`_spawn_preflight_blocking` + `forward_spawn_to_remote`,
`post_agent_terminate`, `deliver_chat_inbound`, `load_checkpoint_messages`,
`agent_roster`, `agent_snapshot`, `get_cluster_status`) — no business logic of its own, no
self-HTTP round-trip (2026-06-07 CLI↔gateway boundary decision). The tool
surface and result shapes are the ones external MCP clients drive.

## Mechanics

- **Flag-gated, additive**: `settings.gateway.mcp_endpoint_enabled`
  (AVA_MCP_ENDPOINT_ENABLED), default off. Off, `/mcp` answers 404 via the
  `mcp_gateway` ASGI wrapper; the existing mcp-daemon path is untouched either way.
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
  `/api/mcp/clients` admin routes. A token outlives every write generation, so
  those routes admit only the human secret or a session it minted; a machine
  token or a runner-minted session gets 403. A no-secret cluster
  still requires an MCP client token; cluster cookies and secrets never count.
  The verified client row belongs to the HTTP request state. Framework-injected
  `Context` passes it explicitly to write-scope checks and business handlers;
  tool arguments and `clientInfo` never supply authority.
  Messages written through this boundary record `mcp_client:<id>` as their
  server-verified credential fact without storing the token.
- **Scope**: `read` clients may list/inspect agents, messages, and cluster
  status. `spawn_agent_guarded_v1`, `send_message`, and `terminate_agent` require `write`.
- **Directory reads**: `list_agents` returns the same bounded scalar-card page
  as REST and stdio MCP. Scope defaults to live; historical search and cursor
  traversal are explicit, with at most 200 rows per call. `get_agent` remains
  the full single-agent diagnostic view.
- **Advertised contract**: `base/api_contracts/mcp_tool_contract.py` owns the
  advertised instructions, the seven tool descriptions (including the
  `caller_protocol` / `idempotency_key` guidance on `send_message`), and message
  projection; local tool signatures still generate the input schemas.
- **Audit**: a `_AuditMiddleware` on the MCPServer records every `tools/call`
  as a `mcp_tool_call` event with client id/name and outcome. Each argument is
  represented only by its JSON type, character size, and SHA-256; raw values
  never enter the event. `agent_id` stays NULL for this service-level identity.

`spawn_agent_guarded_v1` requires a 1–128 character caller key and uses the
existing immutable birth transaction. Its principal-scoped identity binds
`POST /mcp/tools/spawn_agent_guarded_v1`; it is distinct from
guarded HTTP creation. Replay retains the original agent, placement, config,
birth config and launch attempt before mutable preflight. Only its original
pending, unadmitted attempt on its original machine may receive a launch wake.
Rotated, admitted, terminated or deleted births return historical acceptance
without dispatching successor work or inserting another prompt.

Clients keep this exact tool, key and arguments across retries of one intent.
An older server has no such tool and rejects it before creation; a cached tool
list is not admission proof for a later call. The former `spawn_agent` tool is
removed without a compatibility alias. Do not change namespaces after an
ambiguous response. Every call checks the current
verified write credential, including replay. Caller identity arguments are
rejected; JSON-RPC IDs and asserted metadata supply no authority. No automatic
retries, client journal, receipt expiry or legacy-key conversion are added.

Guarded credential creation and its one-time-token recovery exception are owned by
[credential creation receipts](credential-creation.ava.okf.md).

## Why not a router

`/mcp` is not a FastAPI router: the MCP protocol is JSON-RPC over HTTP with its
own lifecycle (initialize handshake), served by the mcp SDK's ASGI app — so it
mounts as a raw ASGI wrapper rather than joining the `/api/*` router set.
