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

from typing import Any, cast

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

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

_INSTRUCTIONS = """\
Ava runs a fleet of long-lived autonomous agents. An agent is a persistent
process with its own conversation history that keeps working after you stop
talking to it — not a request/response endpoint.

The normal loop: `spawn_agent` with a goal (returns immediately, before the
agent has done anything), then `get_agent` / `get_messages` to watch it work,
`send_message` to steer or answer it, `terminate_agent` when it is done. Because
agents work asynchronously, a transcript read right after a spawn is usually
still empty; poll rather than assume failure.

Every tool here acts on one cluster — the one this server was launched from.
There is no cluster argument."""


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


def _compact_agent(row: dict[str, Any]) -> dict[str, Any]:
    """The fields an external agent steers by, out of a full agent record.

    The gateway's row carries the whole lifecycle snapshot (pids, activity
    timestamps, notice counters); handing all of it to a model on every list
    call costs context without changing any decision. `get_agent` returns the
    full record for the cases that need it.
    """
    return {
        "agent_id": row["agent_id"],
        "status": row["status"],
        "label": row["label"],
        "machine": row["machine"],
        "spawner": row["spawner"],
        "last_active_at": row["last_active_at"],
    }


def _message_text(content: Any) -> str:
    """Flatten one message's content to text.

    A model message is either a plain string or a list of typed blocks
    (text / thinking / tool_use / image). Only the text blocks are readable
    prose; the rest is either rendered separately (tool calls, below) or not
    representable in a transcript read.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        blocks: list[Any] = content
        return "\n".join(
            str(cast(dict[str, Any], b)["text"])
            for b in blocks
            if isinstance(b, dict) and cast(dict[str, Any], b).get("type") == "text"
        )
    return str(content)


def _project_message(msg: dict[str, Any]) -> dict[str, Any]:
    """One transcript entry, reduced to role + text + the code the agent ran.

    Ava agents act by writing Python (`execute_code` is their only tool), so the
    code of a turn *is* what the agent did — dropping it would leave a reader
    seeing an agent that talks and never acts. Everything else in the raw
    message (ids, provider metadata, token accounting) is machinery.
    """
    projected: dict[str, Any] = {
        "role": msg["type"],
        "text": _message_text(msg.get("content", "")),
    }
    calls: list[Any] = msg.get("tool_calls") or []
    code = [
        str(c["args"]["code"])
        for c in calls
        if isinstance(c.get("args"), dict) and "code" in c["args"]
    ]
    if code:
        projected["code"] = code
    return projected


def _validate_status(status: str) -> None:
    """Reject a status filter that matches no lifecycle state.

    The filter is applied here rather than by the gateway, so a misremembered
    value ("active", "done") would otherwise come back as an empty list — which
    an external model reads as "the fleet is empty" and acts on. Naming the legal
    set instead lets it correct the call itself.

    Raises:
        ToolError: `status` is not an agent lifecycle state.
    """
    from shared.agents import AgentStatus

    if status not in set(AgentStatus):
        legal = ", ".join(sorted(s.value for s in AgentStatus))
        raise ToolError(f"unknown agent status {status!r}; the states are: {legal}")


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
    server = MCPServer("ava", instructions=_INSTRUCTIONS)

    @server.tool()
    async def list_agents(status: str | None = None) -> list[dict[str, Any]]:
        """List the agents in this Ava cluster with their live state.

        Returns one entry per agent: its id, lifecycle status, label, the machine
        it runs on, who spawned it, and when it was last active. Terminated
        agents are included — pass `status` to narrow to one state, e.g.
        "running" (working right now), "idling" (alive, waiting for input) or
        "terminated" (finished). An unrecognized `status` is an error listing the
        states that exist, never an empty list.
        """
        if status is not None:
            _validate_status(status)
        rows = await _request("GET", "/api/agents", params={"fields": "summary"})
        if status is not None:
            rows = [r for r in rows if r["status"] == status]
        return [_compact_agent(r) for r in rows]

    @server.tool()
    async def get_agent(agent_id: int) -> dict[str, Any]:
        """Read the full state of one agent by id.

        Includes everything `list_agents` returns plus what the agent is doing
        right now and any questions it is blocked on waiting for an answer —
        answer those with `send_message`.
        """
        return await _request("GET", f"/api/agents/{agent_id}")

    @server.tool()
    async def spawn_agent(
        prompt: str,
        label: str | None = None,
        machine: str | None = None,
        config_overlay: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start a new Ava agent and give it a goal. Returns its id immediately.

        The agent begins working asynchronously and keeps running until it
        finishes or is terminated, so write `prompt` as a standing objective
        with whatever context the agent needs, not as a single question. Watch
        its progress with `get_messages`.

        `label` is a short human-readable name shown in the fleet views (one is
        generated if omitted). `machine` picks which host runs it — omit it for
        the default host; a name that is not an agent-runner is rejected.
        `config_overlay` overrides per-agent settings, currently
        `{"llm_model": "<model id>"}`.
        """
        body: dict[str, Any] = {
            "prompt": prompt,
            "prompt_source": _MESSAGE_SOURCE,
            "spawner": _SPAWNER,
            "label": label,
            "machine": machine,
            "config": config_overlay,
        }
        return await _request("POST", "/api/agents", json_body=body, timeout=_SPAWN_TIMEOUT_S)

    @server.tool()
    async def send_message(agent_id: int, content: str) -> dict[str, Any]:
        """Send a message to a running agent — a new instruction, more context,
        or the answer to a question it is blocked on.

        The message is queued and picked up when the agent finishes its current
        step, so this returns before the agent has read it; it does not return
        the agent's reply. Read the reply with `get_messages`. Messaging an
        agent that has already terminated brings it back with its history
        intact.
        """
        body = {"content": content, "source": _MESSAGE_SOURCE}
        return await _request("POST", f"/api/agents/{agent_id}/messages", json_body=body)

    @server.tool()
    async def get_messages(agent_id: int, limit: int = _DEFAULT_MESSAGE_LIMIT) -> dict[str, Any]:
        """Read an agent's conversation history — what it was told and what it
        has said and done.

        Returns the newest `limit` messages, oldest first. Each entry has a
        `role` (human / ai / system), the message `text`, and — for a turn where
        the agent acted — the Python `code` it ran, which is how an Ava agent
        does everything. `total` is the full history length, so a caller can see
        how much was left out.
        """
        payload = await _request("GET", f"/api/agents/{agent_id}/messages", params={"limit": limit})
        return {
            "messages": [_project_message(m) for m in payload["messages"]],
            "total": payload["msg_count"],
        }

    @server.tool()
    async def terminate_agent(
        agent_id: int,
        *,
        message: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """DESTRUCTIVE. End an agent: it stops working and its process exits.

        The agent finishes its current step first, so work in flight is not cut
        off mid-way. `force=True` requests interruption instead; an `enqueued`
        result means accepted, not that the agent or its owned work has exited.
        Use force only when a clean stop cannot progress.

        The agent's history survives either way, and `send_message` revives it,
        so this is reversible; it is destructive in that it stops running work.
        `message` saves a final instruction for that later revival without
        asking the agent to respond before exiting.
        """
        return await _request(
            "POST",
            f"/api/agents/{agent_id}/terminate",
            json_body={"message": message, "force": force},
        )

    @server.tool()
    async def cluster_status() -> dict[str, Any]:
        """Report the health of the Ava cluster itself — which host answered,
        what it is capable of running, and whether it is paused.

        A paused cluster is mid-maintenance: agents are stopped and spawns will
        not run until it resumes. Check this first when the agent tools start
        failing.
        """
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
