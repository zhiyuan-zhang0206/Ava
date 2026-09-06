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
(stdio) are untouched either way. While on, /mcp always requires its own
revocable client token, including on a no-secret cluster; cluster cookies and
the cluster-secret Bearer are deliberately not MCP credentials.

Transport: stateless Streamable HTTP (2026-07-28 protocol revision) — one
fresh transport per POST, no server-side session state, no idle reaping.
Every `tools/call` is recorded as a `mcp_tool_call` audit event with the MCP
client identity and argument schema / character size / SHA-256 only. Raw
argument values never enter the audit stream; agent_id remains NULL because
the client is a service-level identity rather than an Ava agent.

Mounting: `mcp_gateway(app)` is mounted at /mcp (an ASGI wrapper, so the
manager can be swapped per app lifespan); `build_manager(pool)` creates the
server + session manager and is entered (`manager.run()`) by the gateway
lifespan only when the flag is on. The manager cannot run twice, so it is
built fresh per lifespan entry.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any, Literal, cast

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import JSONResponse

from gateway import mcp_clients
from gateway.error_envelope import error_response
from gateway.request_principal import AuthPrincipal, PrincipalScopeError, principal_key
from gateway.routers import agents as _agents_router
from gateway.routers._delivery import deliver_chat_inbound
from gateway.routers.agents_lifecycle import post_agent_terminate
from gateway.schemas import AgentRow, AgentSummary
from ops.agents import get_agent_status
from ops.rpc_schemas import SpawnAgentRequest, TerminateAgentRequest
from shared import agent_snapshot
from shared.agents import AvaAgentError
from shared.audit_events import insert_event_log
from shared.caller_identity import CallerIdentity
from shared.chat_delivery import ClientMessageConflictError
from shared.checkpoint import CheckpointReadError, load_checkpoint_messages
from shared.inbound_provenance import InboundProvenance
from shared.machine import machine_name

# Provenance of everything this surface creates. `spawner` groups the agents
# an external tool created under their own root in the fleet views; `source`
# retains legacy attribution only for non-opted-in clients. This is not evidence
# that an MCP invocation came from a human. V1 derives external provenance from
# the authenticated client row and still requires target protocol admission.
_SPAWNER = "mcp"
_MESSAGE_SOURCE = "user"

# How many of an agent's most recent messages `get_messages` returns by default.
_DEFAULT_MESSAGE_LIMIT = 20

_CURRENT_MCP_CLIENT: ContextVar[dict[str, Any] | None] = ContextVar(
    "current_mcp_client", default=None
)

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


def _compact_agent(row: AgentSummary) -> dict[str, Any]:
    """The fields an external agent steers by, out of a roster summary.

    The gateway summary excludes full-only lifecycle fields before they leave
    Postgres; this compact tool result trims the remaining roster state for a
    model's context. `get_agent` returns the full record for the cases that
    need it. Timestamps serialize to ISO strings so the tool result stays
    JSON-safe.
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
    from mcp.server.mcpserver.exceptions import ToolError

    from shared.agents import AgentStatus

    if status not in set(AgentStatus):
        legal = ", ".join(sorted(s.value for s in AgentStatus))
        raise ToolError(f"unknown agent status {status!r}; the states are: {legal}")


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    raise TypeError(f"MCP argument has non-JSON type {type(value).__name__}")


def _redact_args(args: dict[str, Any]) -> dict[str, dict[str, str | int]]:
    schema: dict[str, str | int] = {}
    size: dict[str, str | int] = {}
    hashes: dict[str, str | int] = {}
    for name, value in args.items():
        serialized = json.dumps(value, ensure_ascii=False)
        schema[name] = _json_type(value)
        size[name] = len(serialized)
        hashes[name] = hashlib.sha256(serialized.encode()).hexdigest()
    return {"schema": schema, "size": size, "sha256": hashes}


def _tool_result_is_error(
    result: HandlerResult,  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
) -> bool:
    from mcp.server.context import HandlerResult

    typed_result = cast(HandlerResult, result)
    is_error = getattr(typed_result, "is_error", False)
    if isinstance(typed_result, dict):
        is_error = typed_result.get("isError", typed_result.get("is_error", False))
    return bool(is_error)


def _validate_mcp_identity_arguments(tool: str, args: dict[str, Any]) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    if tool == "send_message" and args.get("caller_protocol") == "v1":
        reserved = {"source", "instance", "caller_identity", "auth_principal", "client_id"}
        if reserved.intersection(args):
            raise ToolError("caller identity is server-derived; remove identity arguments")


class _AuditMiddleware:
    """Record client identity, outcome, and redacted args for every tools/call."""

    async def __call__(
        self,
        ctx: ServerRequestContext[Any, Any],  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
        call_next: CallNext,  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
    ) -> HandlerResult:  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
        from mcp.server.context import CallNext, HandlerResult, ServerRequestContext

        typed_ctx = cast(ServerRequestContext[Any, Any], ctx)
        typed_call_next = cast(CallNext, call_next)
        result: HandlerResult
        if typed_ctx.method != "tools/call":
            return await typed_call_next(typed_ctx)
        params = dict(typed_ctx.params or {})
        tool = str(params.get("name", "?"))
        raw_args = params.get("arguments")
        args = cast(dict[str, Any], raw_args) if isinstance(raw_args, dict) else {}
        client = _CURRENT_MCP_CLIENT.get()
        if client is None:
            raise RuntimeError("authenticated MCP client context is missing")
        # The id comes from token lookup, never tool arguments or clientInfo.
        # Caller display remains asserted; the authenticated credential binding
        # is recorded separately and does not imply human/Ava agent identity.
        caller = CallerIdentity(kind="external_agent", subject="mcp", instance=str(client["id"]))
        payload: dict[str, Any] = {
            "tool": tool,
            "client_id": client["id"],
            "client_name": client["name"],
            "args": _redact_args(args),
            "auth_principal": {"kind": "mcp_client", "id": client["id"]},
        }
        try:
            _validate_mcp_identity_arguments(tool, args)
            result = await typed_call_next(typed_ctx)
        except Exception as exc:
            insert_event_log(
                event_type="mcp_tool_call",
                agent_id=None,
                source=caller.source(),
                payload=payload
                | {
                    "outcome": "error",
                    "error": type(exc).__name__,
                },
            )
            raise
        is_error = _tool_result_is_error(result)
        insert_event_log(
            event_type="mcp_tool_call",
            agent_id=None,
            source=caller.source(),
            payload=payload
            | (
                {"outcome": "error", "error": "tool call returned an error"}
                if is_error
                else {"outcome": "ok"}
            ),
        )
        return result


def _select_all_blocking(pool: Any) -> list[Any]:
    """Sync snapshot SELECT for list_agents — via to_thread (async handlers
    must not block the event loop)."""
    with pool.connection() as conn:
        return agent_snapshot.select_all(conn, fields="summary")


def _select_one_blocking(pool: Any, agent_id: int) -> Any:
    with pool.connection() as conn:
        return agent_snapshot.select_one(conn, agent_id)


def _require_write_scope(tool: str) -> None:
    """Fail a mutating tool before it reaches any fleet side effect."""
    from mcp.server.mcpserver.exceptions import ToolError

    client = _CURRENT_MCP_CLIENT.get()
    if client is None:
        raise ToolError("authenticated MCP client context is missing")
    if client["scope"] != "write":
        raise ToolError(f"tool {tool!r} requires write scope")


def _register_read_tools(
    server: MCPServer,  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
    pool: Any,
) -> None:
    """Read-side tools: list / inspect / cluster snapshot."""
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError

    typed_server = cast(MCPServer, server)

    @typed_server.tool()
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
        rows = [AgentSummary.model_validate(s.model_dump()) for s in snapshots]
        if status is not None:
            rows = [r for r in rows if r.status == status]
        return [_compact_agent(r) for r in rows]

    @typed_server.tool()
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

    @typed_server.tool()
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


async def _mcp_deliver_send_message(
    pool: Any,
    agent_id: int,
    content: str,
    *,
    caller_protocol: Literal["v1"] | None,
    idempotency_key: str | None,
) -> dict[str, Any]:
    """Deliver an MCP `send_message` call: caller provenance when opted into
    v1, principal-scoped idempotency when a key is supplied.

    Kept out of `_register_fleet_tools` so the tool registration function
    stays under the PLR0915 statement budget; behavior is identical to an
    inline body.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    client = _CURRENT_MCP_CLIENT.get()
    if client is None:
        raise ToolError("authenticated MCP client context is missing")
    source = _MESSAGE_SOURCE
    if caller_protocol == "v1":
        source = CallerIdentity(
            kind="external_agent", subject="mcp", instance=str(client["id"])
        ).source()
    stored_key = None
    if idempotency_key is not None:
        try:
            stored_key = principal_key(
                AuthPrincipal("mcp_client", str(client["id"])),
                "POST",
                f"/api/agents/{agent_id}/messages",
                idempotency_key,
            )
        except PrincipalScopeError as exc:
            raise ToolError(str(exc)) from exc
    # Existence check first (raises AgentNotFound, like the REST route);
    # deliver_chat_inbound then auto-resurrects a terminated target.
    try:
        await asyncio.to_thread(get_agent_status, agent_id)
        delivery = await deliver_chat_inbound(
            pool,
            agent_id,
            prepare=lambda _conn: content,
            source=source,
            payload=None,
            client_message_id=stored_key,
            provenance=InboundProvenance(
                source_verified_by=f"mcp_client:{client['id']}",
                source_transport="http",
            ),
        )
    except (AvaAgentError, ClientMessageConflictError) as exc:
        raise ToolError(str(exc)) from exc
    except HTTPException as exc:
        raise ToolError(str(exc.detail)) from exc
    return {"status": delivery.status, "inbound_id": delivery.inbound_id}


def _register_fleet_tools(
    server: MCPServer,  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
    pool: Any,
) -> None:
    """Fleet-mutating tools: spawn / message / terminate.

    Each tool's description is the contract an external model reads before
    calling it, so it states what the call *does to the fleet* — including
    that `terminate_agent` ends a running process.
    """
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError

    typed_server = cast(MCPServer, server)

    @typed_server.tool()
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
        _require_write_scope("spawn_agent")
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

    @typed_server.tool()
    async def send_message(
        agent_id: int,
        content: str,
        caller_protocol: Literal["v1"] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Send a message to a running agent — a new instruction, more context,
        or the answer to a question it is blocked on.

        The message is queued and picked up when the agent finishes its
        current step, so this returns before the agent has read it; it does
        not return the agent's reply. Read the reply with `get_messages`.
        Messaging an agent that has already terminated brings it back with its
        history intact.

        Explicit caller_protocol='v1' labels the authenticated MCP client as an
        external caller. It adds no permissions and requires an already-live
        target with negotiated v1 support; legacy/default and bootstrap behavior
        are unchanged. Do not supply source or instance: the server owns them.

        An optional idempotency_key identifies a retry of this exact message.
        The server always scopes it to your authenticated MCP client identity;
        another token client using the same key cannot retrieve your receipt.
        """
        _require_write_scope("send_message")
        return await _mcp_deliver_send_message(
            pool,
            agent_id,
            content,
            caller_protocol=caller_protocol,
            idempotency_key=idempotency_key,
        )

    @typed_server.tool()
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

    @typed_server.tool()
    async def terminate_agent(
        agent_id: int,
        *,
        message: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """DESTRUCTIVE. End an agent: it stops working and its process exits.

        The agent finishes its current step first, so work in flight is not
        cut off mid-way. `force=True` kills the process immediately instead,
        losing whatever the current step was doing — use it only for an agent
        that is wedged and will never reach a clean stop.

        The agent's history survives either way, and `send_message` revives
        it, so this is reversible; it is destructive in that it stops running
        work. `message` saves a final instruction for that later revival
        without asking the agent to respond before exiting.
        """
        _require_write_scope("terminate_agent")
        try:
            result = await post_agent_terminate(
                agent_id,
                TerminateAgentRequest(message=message, force=force),
            )
        except AvaAgentError as exc:
            raise ToolError(str(exc)) from exc
        return result.model_dump(mode="json")


def _build_server(pool: Any):  # noqa: ANN202 — inferred from the lazy import
    """Assemble the MCP server: one tool per gateway control route.

    Kept a builder so the manager (and its tool closures over the live
    db_pool) is created per app lifespan — the session manager can only run
    once per instance.
    """
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("ava", instructions=_INSTRUCTIONS, middleware=[_AuditMiddleware()])
    _register_read_tools(server, pool)
    _register_fleet_tools(server, pool)
    return server


def build_manager(pool: Any):  # noqa: ANN201 — inferred from the lazy import
    """Create the /mcp session manager (server + stateless HTTP transport).

    Built fresh per gateway lifespan: `StreamableHTTPSessionManager.run()` can
    only be entered once per instance, and the tools close over the live
    db_pool, which is created by that same lifespan.
    """
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    server = _build_server(pool)
    # The public path builds the manager too: streamable_http_app() constructs
    # it and stores it on the server; session_manager then hands it over. The
    # Starlette sub-app it returns is discarded — the gateway mounts the bare
    # ASGI handler instead (see mcp_gateway).
    # host="" skips the SDK's auto DNS-rebinding guard, which is for standalone
    # loopback servers: this endpoint is embedded in the gateway, its wrapper
    # enforces MCP client auth, and real clients dial it at the machine's
    # reachable hostname (Host: <private-network ip>), which the loopback
    # allowlist would reject with 421.
    server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, host="")
    manager: StreamableHTTPSessionManager = server.session_manager
    return manager


def _bearer_token(scope: dict[str, Any]) -> str | None:
    """Extract the exact Bearer credential from an ASGI HTTP scope."""
    authorization = None
    for name, value in scope.get("headers", []):
        if name.lower() == b"authorization":
            authorization = value.decode("latin-1")
            break
    prefix = "Bearer "
    if authorization is None or not authorization.startswith(prefix):
        return None
    token = authorization[len(prefix) :]
    return token or None


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
        token = _bearer_token(scope)
        client = (
            await asyncio.to_thread(
                mcp_clients.lookup_client_by_token,
                app.state.db_pool,
                token,
            )
            if token is not None
            else None
        )
        if client is None:
            response = JSONResponse(
                {"detail": "invalid or revoked MCP client token"}, status_code=401
            )
            await response(scope, receive, send)
            return
        context_token = _CURRENT_MCP_CLIENT.set(client)
        try:
            await manager.asgi_app(scope, receive, send)
        finally:
            _CURRENT_MCP_CLIENT.reset(context_token)

    return _gateway
