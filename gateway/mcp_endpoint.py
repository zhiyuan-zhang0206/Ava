"""Gateway /mcp endpoint — the cluster control plane as an MCP server.

Design task #1212 step 1: one Streamable HTTP MCP endpoint on the gateway that
every MCP client dials (external tools like Claude Code / Codex today; Ava's
own agents later). The seven tools are thin handlers over the SAME internal
functions the REST routers call — no business logic of its own and no
self-HTTP round-trip (2026-06-07 CLI↔gateway boundary decision: in-process
import for co-located calls, HTTP only to cross a machine).

Flag-gated and additive: `settings.gateway.mcp_endpoint_enabled`
(AVA_MCP_ENDPOINT_ENABLED), default off. While off, /mcp answers 404 and
nothing else changes — the existing mcp-daemon path and `ava mcp serve`
(stdio) are untouched either way. Auth is the cluster middleware like every
route (session cookie or Bearer cluster secret).

Transport: stateless Streamable HTTP (2026-07-28 protocol revision) — one
fresh transport per POST, no server-side session state, no idle reaping.
Every `tools/call` is recorded as a `mcp_tool_call` audit event (agent_id
NULL — service-level event, an external client has no agent identity yet).

Mounting: `mcp_gateway(app)` is mounted at /mcp (an ASGI wrapper, so the
manager can be swapped per app lifespan); `build_manager(pool)` creates the
server + session manager and is entered (`manager.run()`) by the gateway
lifespan only when the flag is on. The manager cannot run twice, so it is
built fresh per lifespan entry.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

from fastapi import FastAPI, Request
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from gateway.error_envelope import error_response
from gateway.routers import agents as _agents_router
from gateway.routers._delivery import deliver_chat_inbound
from gateway.routers.agents_lifecycle import post_agent_terminate
from gateway.schemas import AgentRow
from ops.agents import get_agent_status
from ops.rpc_schemas import SpawnAgentRequest, TerminateAgentRequest
from shared import agent_snapshot
from shared.agents import AvaAgentError
from shared.audit_events import insert_event_log
from shared.checkpoint import CheckpointReadError, load_checkpoint_messages
from shared.machine import machine_name

# Provenance of everything this surface creates. `spawner` groups the agents
# an external tool created under their own root in the fleet views; `source`
# must be one of the envelope's legal kinds (shared/envelope.py), and "user"
# is the honest one: an MCP client acts for the human driving it.
_SPAWNER = "mcp"
_MESSAGE_SOURCE = "user"

# How many of an agent's most recent messages `get_messages` returns by default.
_DEFAULT_MESSAGE_LIMIT = 20

_INSTRUCTIONS = """
Ava runs a fleet of long-lived autonomous agents. An agent is a persistent
process with its own conversation history that keeps working after you stop
talking to it — not a request/response endpoint.

The normal loop: `spawn_agent` with a goal (returns immediately, before the
agent has done anything), then `get_agent` / `get_messages` to watch it work,
`send_message` to steer or answer it, `terminate_agent` when it is done. Because
agents work asynchronously, a transcript read right after a spawn is usually
still empty; poll rather than assume failure.

Every tool here acts on one cluster — the one this gateway belongs to.
There is no cluster argument."""


def _compact_agent(row: AgentRow) -> dict[str, Any]:
    """The fields an external agent steers by, out of a full agent record.

    The full row carries the whole lifecycle snapshot (pids, activity
    timestamps, notice counters); handing all of it to a model on every list
    call costs context without changing any decision. `get_agent` returns the
    full record for the cases that need it. Timestamps serialize to ISO
    strings so the tool result stays JSON-safe.
    """
    return {
        "agent_id": row.agent_id,
        "status": row.status,
        "label": row.label,
        "machine": row.machine,
        "spawner": row.spawner,
        "last_active_at": row.last_active_at.isoformat() if row.last_active_at else None,
    }


def _message_text(content: Any) -> str:
    """Flatten one message's content to text (str, or list of typed blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(cast(dict[str, Any], block)["text"])
            for block in content
            if isinstance(block, dict) and cast(dict[str, Any], block).get("type") == "text"
        )
    return str(content)


def _project_message(msg: dict[str, Any]) -> dict[str, Any]:
    """One transcript entry, reduced to role + text + the code the agent ran.

    Ava agents act by writing Python (`execute_code` is their only tool), so
    the code of a turn *is* what the agent did — dropping it would leave a
    reader seeing an agent that talks and never acts.
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
    """Reject a status filter that matches no lifecycle state (an unrecognized
    value must be an error listing the legal states, never an empty list)."""
    from shared.agents import AgentStatus

    if status not in set(AgentStatus):
        legal = ", ".join(sorted(s.value for s in AgentStatus))
        raise ToolError(f"unknown agent status {status!r}; the states are: {legal}")


class _AuditMiddleware:
    """Record every tools/call on this endpoint as an audit event.

    Registered on the MCPServer so auditing is one place, not seven
    try/excepts: outcome ok/error and the error text ride in the payload.
    `agent_id` is None — an external MCP client has no agent identity (the
    client_key model from design task #1212 lands with the machine-routing
    step); the events table takes NULL agent_id for service-level events.
    """

    async def __call__(
        self, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        if ctx.method != "tools/call":
            return await call_next(ctx)
        params = dict(ctx.params or {})
        tool = str(params.get("name", "?"))
        raw_args = params.get("arguments")
        args = cast(dict[str, Any], raw_args) if isinstance(raw_args, dict) else {}
        try:
            result = await call_next(ctx)
        except Exception as exc:
            insert_event_log(
                event_type="mcp_tool_call",
                agent_id=None,
                source="mcp",
                payload={
                    "tool": tool,
                    "args": args,
                    "outcome": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            raise
        insert_event_log(
            event_type="mcp_tool_call",
            agent_id=None,
            source="mcp",
            payload={"tool": tool, "args": args, "outcome": "ok"},
        )
        return result


def _select_all_blocking(pool: Any) -> list[Any]:
    """Sync snapshot SELECT for list_agents — via to_thread (async handlers
    must not block the event loop)."""
    with pool.connection() as conn:
        return agent_snapshot.select_all(conn)


def _select_one_blocking(pool: Any, agent_id: int) -> Any:
    with pool.connection() as conn:
        return agent_snapshot.select_one(conn, agent_id)


def _register_read_tools(server: MCPServer, pool: Any) -> None:
    """Read-side tools: list / inspect / cluster snapshot."""

    @server.tool()
    async def list_agents(status: str | None = None) -> list[dict[str, Any]]:
        """List the agents in this Ava cluster with their live state.

        Returns one entry per agent: its id, lifecycle status, label, the
        machine it runs on, who spawned it, and when it was last active.
        Terminated agents are included — pass `status` to narrow to one state,
        e.g. "running" (working right now), "idling" (alive, waiting for
        input) or "terminated" (finished). An unrecognized `status` is an
        error listing the states that exist, never an empty list.
        """
        if status is not None:
            _validate_status(status)
        snapshots = await asyncio.to_thread(_select_all_blocking, pool)
        rows = [AgentRow.model_validate(s.model_dump()) for s in snapshots]
        if status is not None:
            rows = [r for r in rows if r.status == status]
        return [_compact_agent(r) for r in rows]

    @server.tool()
    async def get_agent(agent_id: int) -> dict[str, Any]:
        """Read the full state of one agent by id.

        Includes everything `list_agents` returns plus what the agent is
        doing right now and any questions it is blocked on waiting for an
        answer — answer those with `send_message`.
        """
        snap = await asyncio.to_thread(_select_one_blocking, pool, agent_id)
        if snap is None:
            raise ToolError(f"agent {agent_id} does not exist")
        return AgentRow.model_validate(snap.model_dump()).model_dump(mode="json")

    @server.tool()
    async def cluster_status() -> dict[str, Any]:
        """Report the health of the Ava cluster itself — which host answered,
        what it is capable of running, and whether it is paused.

        A paused cluster is mid-maintenance: agents are stopped and spawns
        will not run until it resumes. Check this first when the agent tools
        start failing.
        """
        from gateway.routers.cluster import get_cluster_status

        snapshot = await get_cluster_status()
        return snapshot.model_dump(mode="json")


def _register_fleet_tools(server: MCPServer, pool: Any) -> None:
    """Fleet-mutating tools: spawn / message / terminate.

    Each tool's description is the contract an external model reads before
    calling it, so it states what the call *does to the fleet* — including
    that `terminate_agent` ends a running process.
    """

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

        `label` is a short human-readable name shown in the fleet views (one
        is generated if omitted). `machine` picks which host runs it — omit it
        for the default host; a name that is not an agent-runner is rejected.
        `config_overlay` overrides per-agent settings, currently
        `{"llm_model": "<model id>"}`.
        """
        body = SpawnAgentRequest(
            prompt=prompt,
            prompt_source=_MESSAGE_SOURCE,
            spawner=_SPAWNER,
            label=label,
            machine=machine,
            config=config_overlay,
        )
        target = body.machine if body.machine is not None else machine_name()
        # Same path as POST /api/agents: `create_and_launch_agent` (preflight +
        # row creation off the event loop, then the launch op). Referenced
        # through the router module so tests patch the same seam as the REST
        # spawn route.
        try:
            spawned = await _agents_router.create_and_launch_agent(body, target, pool)
        except AvaAgentError as exc:
            # The old stdio serve forwarded the gateway's `detail` verbatim;
            # in-process the same business errors are AvaAgentError instances —
            # surface their message as a tool error, not a protocol error.
            raise ToolError(str(exc)) from exc
        return spawned.model_dump(mode="json")

    @server.tool()
    async def send_message(agent_id: int, content: str) -> dict[str, Any]:
        """Send a message to a running agent — a new instruction, more context,
        or the answer to a question it is blocked on.

        The message is queued and picked up when the agent finishes its
        current step, so this returns before the agent has read it; it does
        not return the agent's reply. Read the reply with `get_messages`.
        Messaging an agent that has already terminated brings it back with its
        history intact.
        """
        # Existence check first (raises AgentNotFound, like the REST route);
        # deliver_chat_inbound then auto-resurrects a terminated target.
        try:
            await asyncio.to_thread(get_agent_status, agent_id)
            delivery = await deliver_chat_inbound(
                pool,
                agent_id,
                prepare=lambda _conn: content,
                source=_MESSAGE_SOURCE,
                payload=None,
            )
        except AvaAgentError as exc:
            raise ToolError(str(exc)) from exc
        return {"status": delivery.status}

    @server.tool()
    async def get_messages(agent_id: int, limit: int = _DEFAULT_MESSAGE_LIMIT) -> dict[str, Any]:
        """Read an agent's conversation history — what it was told and what it
        has said and done.

        Returns the newest `limit` messages, oldest first. Each entry has a
        `role` (human / ai / system), the message `text`, and — for a turn
        where the agent acted — the Python `code` it ran, which is how an Ava
        agent does everything. `total` is the full history length, so a caller
        can see how much was left out.
        """
        from shared.db import agent_exists

        def _exists() -> bool:
            with pool.connection() as conn:
                return agent_exists(conn, agent_id)

        if not await asyncio.to_thread(_exists):
            raise ToolError(f"agent {agent_id} does not exist")
        try:
            messages = await asyncio.to_thread(load_checkpoint_messages, agent_id)
        except CheckpointReadError as exc:
            raise ToolError(f"checkpoint read failed; retry or check store health: {exc}") from exc
        window = messages[-limit:]
        return {
            "messages": [_project_message(m.model_dump()) for m in window],
            "total": len(messages),
        }

    @server.tool()
    async def terminate_agent(agent_id: int, *, force: bool = False) -> dict[str, Any]:
        """DESTRUCTIVE. End an agent: it stops working and its process exits.

        The agent finishes its current step first, so work in flight is not
        cut off mid-way. `force=True` kills the process immediately instead,
        losing whatever the current step was doing — use it only for an agent
        that is wedged and will never reach a clean stop.

        The agent's history survives either way, and `send_message` revives
        it, so this is reversible; it is destructive in that it stops running
        work.
        """
        try:
            result = await post_agent_terminate(agent_id, TerminateAgentRequest(force=force))
        except AvaAgentError as exc:
            raise ToolError(str(exc)) from exc
        return result.model_dump(mode="json")


def _build_server(pool: Any) -> MCPServer:
    """Assemble the MCP server: one tool per gateway control route.

    Kept a builder so the manager (and its tool closures over the live
    db_pool) is created per app lifespan — the session manager can only run
    once per instance.
    """
    server = MCPServer("ava", instructions=_INSTRUCTIONS, middleware=[_AuditMiddleware()])
    _register_read_tools(server, pool)
    _register_fleet_tools(server, pool)
    return server


def build_manager(pool: Any) -> StreamableHTTPSessionManager:
    """Create the /mcp session manager (server + stateless HTTP transport).

    Built fresh per gateway lifespan: `StreamableHTTPSessionManager.run()` can
    only be entered once per instance, and the tools close over the live
    db_pool, which is created by that same lifespan.
    """
    server = _build_server(pool)
    # The public path builds the manager too: streamable_http_app() constructs
    # it and stores it on the server; session_manager then hands it over. The
    # Starlette sub-app it returns is discarded — the gateway mounts the bare
    # ASGI handler instead (see mcp_gateway).
    # host="" skips the SDK's auto DNS-rebinding guard, which is for standalone
    # loopback servers: this endpoint is embedded in the gateway and sits
    # behind the cluster auth middleware, and real clients dial it at the
    # machine's reachable hostname (Host: <private-network ip>), which the loopback
    # allowlist would reject with 421.
    server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, host="")
    return server.session_manager


def mcp_gateway(app: FastAPI) -> Callable[..., Awaitable[None]]:
    """ASGI wrapper mounted at /mcp — 404 while the endpoint is disabled.

    Reads the manager off app.state on every request so a disabled (or
    shut-down) lifespan answers 404 exactly like the grafana proxy answers
    while off, instead of the route being absent (two different "off"
    behaviors for the same flag flip).
    """

    async def _gateway(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        manager = getattr(app.state, "mcp_manager", None)
        if manager is None:
            response = error_response(
                Request(scope),
                code="mcp_endpoint_disabled",
                status=404,
                detail="mcp endpoint is disabled",
                retryable=False,
            )
            await response(scope, receive, send)
            return
        await manager.asgi_app(scope, receive, send)

    return _gateway
