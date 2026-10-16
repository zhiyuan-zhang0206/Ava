# Connecting external MCP clients to the gateway `/mcp` endpoint

The gateway exposes the cluster control plane as a standard MCP server over
[Streamable HTTP] at `/mcp` (implementation
[`gateway/mcp_server/endpoint.py`](../../../gateway/mcp_server/docs/mcp-endpoint/mcp-endpoint.ava.okf.md)). Any MCP
client — Claude Code, Codex, anything speaking the protocol — drives the fleet
through it with seven control tools
(list_agents / get_agent / spawn_agent_guarded_v1 / send_message / get_messages /
terminate_agent / cluster_status).

[Streamable HTTP]: https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#streamable-http

## Enable the endpoint

The endpoint ships **off** — flag-gated so a cluster that does not use it is
completely unaffected (off, `/mcp` answers 404):

```bash
# in the gateway home's .env, on the gateway host:
AVA_MCP_ENDPOINT_ENABLED=true
# then restart the gateway: the flag is read at gateway start
```

The flag decides whether MCP client tokens authenticate at all, and those
tokens outlive every write generation, so no config write sets it: `ava config
set` (API or `--local`) refuses it and the settings panel shows it read-only,
as for `AVA_ALERTS_WEBHOOK_TOKEN`. Edit the gateway `.env` on its host, after
the rollout that ships the endpoint code.

## Connect a client

Create one credential per external client through the admin API, with the
human cluster secret (or from a browser session logged in with it). A client
token outlives every write generation, so a machine API token or a
runner-minted session gets 403 here. Choose `read` for inspection only, or
`write` when the client must spawn, message, or terminate agents. Creation is
keyed: send a fresh `Idempotency-Key` (1-128 characters) with
`Idempotency-Scope: principal-v1`, and reuse that key only to retry the same
request after an ambiguous response. The plaintext token appears only in the
first response; a replay of the same key returns the original metadata with
`replayed: true` and `token: null`, because the gateway stores only the SHA-256
hash. If the first response was lost, revoke that client and create a new one
under a new key and an unused name:

```bash
curl -s http://<gateway-host>/api/keyed/v1/mcp/clients \
  -H "Authorization: Bearer <cluster-secret>" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Idempotency-Scope: principal-v1" \
  -H "Content-Type: application/json" \
  -d '{"name":"claude","scope":"write"}'
```

Configure the client with the returned `token`, not the cluster secret:

```bash
claude mcp add --transport http ava http://<gateway-host>/mcp \
  --header "Authorization: Bearer <mcp-client-token>"
```

Codex takes the same shape (`codex mcp add`). `<gateway-host>` is the address
the cluster advertises (`AVA_GATEWAY_URL`). `/mcp` requires a client token; a session cookie or cluster-secret Bearer is rejected.

List client metadata with `GET /api/mcp/clients`. Revoke one immediately with
`POST /api/mcp/clients/<id>/revoke`; a revoked token receives 401 thereafter.

## Verify

```bash
curl -s http://<gateway-host>/mcp \
  -H "Authorization: Bearer <mcp-client-token>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

A healthy endpoint answers an SSE stream whose single `data:` frame carries the
seven tools. A cluster with the flag off gets 404; a missing, invalid, or
revoked client token gets 401.

## Notes

- **Stateless**: each POST is one self-contained JSON-RPC message; the endpoint
  keeps no server-side session state, so clients must not rely on
  `Mcp-Session-Id` resumability.
- **Audited**: every `tools/call` lands in the unified event stream as an
  `mcp_tool_call` audit event with the client id/name and outcome. Argument
  values are never recorded — only each argument's JSON type, character size,
  and SHA-256. `agent_id` is NULL because the client identity is service-level.

Agent creation uses `spawn_agent_guarded_v1` with a required 1–128 character
`idempotency_key`. Retain the same key and arguments to recover the original
birth after response loss. The former `spawn_agent` tool has been removed
without a compatibility alias; do not substitute another creation entry for
the same intent. See the endpoint owner for retained acceptance semantics.
