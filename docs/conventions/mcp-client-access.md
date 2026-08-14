# Connecting external MCP clients to the gateway `/mcp` endpoint

The gateway exposes the cluster control plane as a standard MCP server over
[Streamable HTTP] at `/mcp` (design task #1212 step 1; implementation
[`gateway/mcp_endpoint.py`](../../gateway/mcp_endpoint.ava.okf.md)). Any MCP
client — Claude Code, Codex, anything speaking the protocol — drives the fleet
through it: the same seven control tools the stdio `ava mcp serve` offered
(list_agents / get_agent / spawn_agent / send_message / get_messages /
terminate_agent / cluster_status), same result shapes, same error semantics.

[Streamable HTTP]: https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#streamable-http

## Enable the endpoint

The endpoint ships **off** — flag-gated so a cluster that does not use it is
completely unaffected (off, `/mcp` answers 404):

```bash
ava config set AVA_MCP_ENDPOINT_ENABLED=true
# restart_required=gateway: the flag is read at gateway start
```

Enabling it is a cluster config change (`cluster-pinned` scope); do it after
the rollout that ships the endpoint code.

## Connect a client

Auth is the same cluster middleware as every API route: a valid session cookie
or `Authorization: Bearer <cluster secret>`. A remote MCP client has no cookie,
so it presents the secret (the same credential the SDK and the CLI present):

```bash
claude mcp add --transport http ava http://<gateway-host>/mcp   --header "Authorization: Bearer <cluster-secret>"
```

Codex takes the same shape (`codex mcp add`). `<gateway-host>` is the address
the cluster advertises (`AVA_GATEWAY_URL`); never expose the endpoint beyond
that network — the bearer is a full-control credential for the cluster.

## Verify

```bash
curl -s http://<gateway-host>/mcp \
  -H "Authorization: Bearer <cluster-secret>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

A healthy endpoint answers an SSE stream whose single `data:` frame carries the
seven tools. A cluster with the flag off (or a missing bearer) gets a plain
404/401 JSON body instead.

## Notes

- **Stateless**: each POST is one self-contained JSON-RPC message; the endpoint
  keeps no server-side session state, so clients must not rely on
  `Mcp-Session-Id` resumability.
- **Audited**: every `tools/call` lands in the unified event stream as an
  `mcp_tool_call` audit event (tool, args, outcome). `agent_id` is NULL —
  external clients have no agent identity; a per-client `client_key` model
  arrives with the machine-routing step.
- **The stdio form is deprecated**: `ava mcp serve` keeps working unchanged
  until the machine-routing steps land, then retires; new integrations should
  point at `/mcp`.
