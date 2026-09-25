"""`ava mcp serve` — this cluster's control plane, exposed as an MCP server.

Deprecation notice (design task #1212 step 1): the control plane is moving
onto the gateway as a Streamable HTTP endpoint (`/mcp`, flag
AVA_MCP_ENDPOINT_ENABLED) — external MCP clients will point directly at
`https://<gateway>/mcp` and this stdio form will be retired once the
machine-routing steps land. Until then behavior is unchanged: the same seven
tools over stdio.

The gateway already owns every control effect (spawn / message / inspect /
terminate) behind its authenticated HTTP API. This module is a **thin proxy**:
one MCP tool per gateway route, no logic of its own, so an external agent
(Claude Code, Codex, any MCP client) drives the same cluster the web UI and the
`ava` CLI drive, with the same auth and the same failure modes.

Which cluster it drives is not a parameter: `shared.machine.gateway_api_base`
resolves the gateway of the checkout this `ava` belongs to, and
`gateway_auth_headers` presents that cluster's secret. So the prod `ava` on PATH
serves prod, and a worktree's `.venv/bin/ava` serves that worktree's cluster —
the same rule every other verb follows.

Transport is stdio: **stdout is the JSON-RPC channel**, so nothing on this path
may print. Diagnostics go to stderr (where `shared.log.logger` already writes).

The client-side `ava mcp` verbs (install / add / list / ...) manage servers Ava's
own agents connect *out* to; `serve` is the opposite direction — Ava as the
server. Both live under one noun because both are "MCP wiring for this machine".
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, cast

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from shared.api_contracts.mcp_tool_contract import (
    project_message,
    server_instructions,
    tool_description,
)

# Read calls are a single gateway round-trip; a spawn additionally launches a
# process on the target machine, so it gets its own longer budget.
_TIMEOUT_S = 30.0
_SPAWN_TIMEOUT_S = 90.0

# Provenance of everything this server creates. `spawner` is free-form and
# groups these agents under their own root in the fleet views, so an operator
# can see at a glance which agents an external tool created. `source` must be
# one of the envelope's legal kinds (shared/envelope.py:validate_source), and
# `user` is the honest one: an MCP client acts for the human driving it, and the
# receiving agent should read the message exactly as it reads one typed into the
# web UI.
_SPAWNER = "mcp"
_MESSAGE_SOURCE = "user"

# How many of an agent's most recent messages `get_messages` returns by default.
_DEFAULT_MESSAGE_LIMIT = 20


def _detail_item(item: Any) -> str:
    """One pydantic 422 entry: `loc.path: msg` (non-dict entries as-is)."""
    if not isinstance(item, dict):
        return str(item)
    d = cast(dict[str, Any], item)
    loc = ".".join(str(p) for p in d.get("loc", []))
    return f"{loc}: {d.get('msg', d)}"


def _detail(resp: httpx.Response) -> str:
    """The human-readable reason out of a gateway error response.

    The gateway answers errors in three shapes: its own wire error
    (`{"detail": ..., "reason": ...}`), a plain `HTTPException`
    (`{"detail": "..."}`), and pydantic's 422 (`{"detail": [{...}, ...]}`).
    All three carry `detail`; the list form is joined so the caller sees which
    field it got wrong instead of a stringified list of dicts.
    """
    try:
        body: dict[str, Any] = resp.json()
    except ValueError:
        return resp.text.strip() or f"HTTP {resp.status_code}"
    if not isinstance(body, dict) or "detail" not in body:
        return resp.text.strip() or f"HTTP {resp.status_code}"
    detail = body["detail"]
    if isinstance(detail, list):
        return "; ".join(_detail_item(i) for i in cast(list[Any], detail))
    return str(detail)


async def _request(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = _TIMEOUT_S,
) -> Any:
    """One authenticated call to this cluster's gateway; returns the parsed body.

    Every failure becomes a `ToolError`, which the MCP client sees as a tool
    error result carrying this message — the only channel an external agent has
    for learning what went wrong, so the gateway's own `detail` is forwarded
    verbatim rather than replaced by a generic string. Nothing is retried and
    nothing is swallowed: a 404 for a missing agent and an unreachable gateway
    must both reach the caller as failures, not as empty results.

    Raises:
        ToolError: the gateway rejected the call, or could not be reached.
    """
    from shared.machine import gateway_api_base, gateway_auth_headers

    url = f"{gateway_api_base()}{path}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
            resp = await client.request(
                method, url, json=json_body, params=params, headers=gateway_auth_headers()
            )
    except httpx.HTTPError as exc:
        raise ToolError(f"gateway unreachable at {url}: {exc!r}") from exc

    if resp.status_code >= 400:
        raise ToolError(f"gateway rejected {method} {path} ({resp.status_code}): {_detail(resp)}")
    if resp.status_code == 204 or not resp.content:
        return None
    return resp.json()


def build_server() -> MCPServer:
    """Assemble the MCP server: one tool per gateway control route.

    Kept a builder rather than a module-level singleton so tests can construct a
    server, list its tools and call them without a live gateway, and so nothing
    is registered as an import side effect.

    Each tool's description is the contract an external model reads before
    calling it, so it states what the call *does to the fleet* — including that
    `terminate_agent` ends a running process — and never how the proxy reaches
    the gateway.
    """
    server = MCPServer("ava", instructions=server_instructions("stdio"))

    @server.tool(description=tool_description("list_agents", "stdio"))
    async def list_agents(
        scope: Literal["live", "terminated", "all"] = "live",
        query: Annotated[str, Field(max_length=200)] = "",
        before_id: Annotated[int | None, Field(ge=1, le=9223372036854775807)] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 100,
    ) -> dict[str, Any]:
        params: dict[str, str | int] = {"scope": scope, "query": query, "limit": limit}
        if before_id is not None:
            params["before_id"] = before_id
        return await _request("GET", "/api/agents", params=params)

    @server.tool(description=tool_description("get_agent", "stdio"))
    async def get_agent(agent_id: int) -> dict[str, Any]:
        return await _request("GET", f"/api/agents/{agent_id}")

    @server.tool(description=tool_description("spawn_agent", "stdio"))
    async def spawn_agent(
        prompt: str,
        label: str | None = None,
        machine: str | None = None,
        config_overlay: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "prompt": prompt,
            "prompt_source": _MESSAGE_SOURCE,
            "spawner": _SPAWNER,
            "label": label,
            "machine": machine,
            "config": config_overlay,
        }
        return await _request("POST", "/api/agents", json_body=body, timeout=_SPAWN_TIMEOUT_S)

    @server.tool(description=tool_description("send_message", "stdio"))
    async def send_message(agent_id: int, content: str) -> dict[str, Any]:
        body = {"content": content, "source": _MESSAGE_SOURCE}
        return await _request("POST", f"/api/agents/{agent_id}/messages", json_body=body)

    @server.tool(description=tool_description("get_messages", "stdio"))
    async def get_messages(agent_id: int, limit: int = _DEFAULT_MESSAGE_LIMIT) -> dict[str, Any]:
        payload = await _request("GET", f"/api/agents/{agent_id}/messages", params={"limit": limit})
        return {
            "messages": [project_message(m) for m in payload["messages"]],
            "total": payload["msg_count"],
        }

    @server.tool(description=tool_description("terminate_agent", "stdio"))
    async def terminate_agent(
        agent_id: int,
        *,
        message: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        return await _request(
            "POST",
            f"/api/agents/{agent_id}/terminate",
            json_body={"message": message, "force": force},
        )

    @server.tool(description=tool_description("cluster_status", "stdio"))
    async def cluster_status() -> dict[str, Any]:
        return await _request("GET", "/api/cluster/status")

    return server


def cmd_mcp_serve() -> int:
    """`ava mcp serve` — run the MCP server on stdio until the client disconnects.

    Blocks; a client (`claude mcp add ava -- ava mcp serve`) owns the process
    lifetime. Returns 0 on a clean disconnect. Fails fast if this checkout has no
    gateway configured, rather than starting a server whose every tool would
    error one call later.
    """
    from shared.machine import gateway_api_base

    # Resolve up front: an unconfigured checkout is an install problem, and the
    # message is far more useful on the terminal that ran `serve` than buried in
    # a tool error the client shows much later.
    gateway_api_base()
    build_server().run(transport="stdio")
    return 0
