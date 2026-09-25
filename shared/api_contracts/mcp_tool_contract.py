"""Shared MCP message projection and advertised contract for both inbound surfaces."""

from __future__ import annotations

from typing import Any, Literal, cast

Surface = Literal["stdio", "gateway"]

# The SDK uses the explicit description verbatim, including indentation and
# trailing whitespace. The original stdio docstrings are the shared prose.
_TOOL_DESCRIPTIONS: dict[str, str] = {
    "list_agents": """Read one agent directory page, newest IDs first.

        `live` includes all nonterminated agents; use `terminated` for history
        or `all` for both. `query` matches a label substring or an exact agent
        ID. Returns `agents` and `next_cursor`: pass that cursor as `before_id`
        with the same filters to continue. None means no further results.
        Each call reads at most `limit` agents (1 through 200).
        """,
    "get_agent": """Read the full state of one agent by id.

        Includes lifecycle details, what the agent is doing right now, and any
        questions it is blocked on waiting for an answer —
        answer those with `send_message`.
        """,
    "spawn_agent": """Start a new Ava agent and give it a goal. Returns its id immediately.

        The agent begins working asynchronously and keeps running until it
        finishes or is terminated, so write `prompt` as a standing objective
        with whatever context the agent needs, not as a single question. Watch
        its progress with `get_messages`.

        `label` is a short human-readable name shown in the fleet views (one is
        generated if omitted). `machine` picks which host runs it — omit it for
        the default host; a name that is not an agent-runner is rejected.
        `config_overlay` overrides per-agent settings, currently
        `{"llm_model": "<model id>"}`.
        """,
    "send_message": """Send a message to a running agent — a new instruction, more context,
        or the answer to a question it is blocked on.

        The message is queued and picked up when the agent finishes its current
        step, so this returns before the agent has read it; it does not return
        the agent's reply. Read the reply with `get_messages`. Messaging an
        agent that has already terminated brings it back with its history
        intact.
        """,
    "get_messages": """Read an agent's conversation history — what it was told and what it
        has said and done.

        Returns the newest `limit` messages, oldest first. Each entry has a
        `role` (human / ai / system), the message `text`, and — for a turn where
        the agent acted — the Python `code` it ran, which is how an Ava agent
        does everything. `total` is the full history length, so a caller can see
        how much was left out.
        """,
    "terminate_agent": """DESTRUCTIVE. End an agent: it stops working and its process exits.

        The agent finishes its current step first, so work in flight is not cut
        off mid-way. `force=True` requests interruption instead; an `enqueued`
        result means accepted, not that the agent or its owned work has exited.
        Use force only when a clean stop cannot progress.

        The agent's history survives either way, and `send_message` revives it,
        so this is reversible; it is destructive in that it stops running work.
        `message` saves a final instruction for that later revival without
        asking the agent to respond before exiting. The result also reports
        `open_tasks` — the tasks the agent still owns as it goes down (at most
        five, most recently updated first; null when it owns none).
        """,
    "cluster_status": """Report the health of the Ava cluster itself — which host answered,
        what it is capable of running, and whether it is paused.

        A paused cluster is mid-maintenance: agents are stopped and spawns will
        not run until it resumes. Check this first when the agent tools start
        failing.
        """,
}

# Existing gateway line breaks differ from stdio; keep those wire bytes.
_GATEWAY_REWRAPS: dict[str, tuple[tuple[str, str], ...]] = {
    "get_agent": (
        (
            (
                "        questions it is blocked on waiting for an answer —\n"
                "        answer those with `send_message`.\n"
            ),
            (
                "        questions it is blocked on waiting for an answer — answer those with `send_message`.\n"
            ),
        ),
    ),
    "spawn_agent": (
        (
            (
                "        `label` is a short human-readable name shown in the fleet views (one is\n"
                "        generated if omitted). `machine` picks which host runs it — omit it for\n"
                "        the default host; a name that is not an agent-runner is rejected.\n"
            ),
            (
                "        `label` is a short human-readable name shown in the fleet views (one\n"
                "        is generated if omitted). `machine` picks which host runs it — omit it\n"
                "        for the default host; a name that is not an agent-runner is rejected.\n"
            ),
        ),
    ),
    "send_message": (
        (
            (
                "        The message is queued and picked up when the agent finishes its current\n"
                "        step, so this returns before the agent has read it; it does not return\n"
                "        the agent's reply. Read the reply with `get_messages`. Messaging an\n"
                "        agent that has already terminated brings it back with its history\n"
                "        intact.\n"
            ),
            (
                "        The message is queued and picked up when the agent finishes its\n"
                "        current step, so this returns before the agent has read it; it does\n"
                "        not return the agent's reply. Read the reply with `get_messages`.\n"
                "        Messaging an agent that has already terminated brings it back with its\n"
                "        history intact.\n"
            ),
        ),
    ),
    "get_messages": (
        (
            (
                "        `role` (human / ai / system), the message `text`, and — for a turn where\n"
                "        the agent acted — the Python `code` it ran, which is how an Ava agent\n"
                "        does everything. `total` is the full history length, so a caller can see\n"
                "        how much was left out.\n"
            ),
            (
                "        `role` (human / ai / system), the message `text`, and — for a turn\n"
                "        where the agent acted — the Python `code` it ran, which is how an Ava\n"
                "        agent does everything. `total` is the full history length, so a caller\n"
                "        can see how much was left out.\n"
            ),
        ),
    ),
    "terminate_agent": (
        (
            (
                "        The agent finishes its current step first, so work in flight is not cut\n"
                "        off mid-way. `force=True` requests interruption instead; an `enqueued`\n"
                "        result means accepted, not that the agent or its owned work has exited.\n"
                "        Use force only when a clean stop cannot progress.\n"
            ),
            (
                "        The agent finishes its current step first, so work in flight is not\n"
                "        cut off mid-way. `force=True` requests interruption instead; an\n"
                "        `enqueued` result means accepted, not that the agent or its owned work\n"
                "        has exited. Use force only when a clean stop cannot progress.\n"
            ),
        ),
        (
            (
                "        The agent's history survives either way, and `send_message` revives it,\n"
                "        so this is reversible; it is destructive in that it stops running work.\n"
                "        `message` saves a final instruction for that later revival without\n"
                "        asking the agent to respond before exiting. The result also reports\n"
                "        `open_tasks` — the tasks the agent still owns as it goes down (at most\n"
                "        five, most recently updated first; null when it owns none).\n"
            ),
            (
                "        The agent's history survives either way, and `send_message` revives\n"
                "        it, so this is reversible; it is destructive in that it stops running\n"
                "        work. `message` saves a final instruction for that later revival\n"
                "        without asking the agent to respond before exiting. The result also\n"
                "        reports `open_tasks` — the tasks the agent still owns as it goes down\n"
                "        (at most five, most recently updated first; null when it owns none).\n"
            ),
        ),
    ),
    "cluster_status": (
        (
            (
                "        A paused cluster is mid-maintenance: agents are stopped and spawns will\n"
                "        not run until it resumes. Check this first when the agent tools start\n"
                "        failing.\n"
            ),
            (
                "        A paused cluster is mid-maintenance: agents are stopped and spawns\n"
                "        will not run until it resumes. Check this first when the agent tools\n"
                "        start failing.\n"
            ),
        ),
    ),
}

_GATEWAY_SEND_MESSAGE_APPENDIX = (
    "\n"
    "\n"
    "        Explicit caller_protocol='v1' labels the authenticated MCP client as an\n"
    "        external caller. It adds no permissions and requires an already-live\n"
    "        target with negotiated v1 support; legacy/default and bootstrap behavior\n"
    "        are unchanged. Do not supply source or instance: the server owns them.\n"
    "\n"
    "        An optional idempotency_key identifies a retry of this exact message.\n"
    "        The server always scopes it to your authenticated MCP client identity;\n"
    "        another token client using the same key cannot retrieve your receipt.\n"
    "        "
)

_COMMON_INSTRUCTIONS = (
    "Ava runs a fleet of long-lived autonomous agents. An agent is a persistent\n"
    "process with its own conversation history that keeps working after you stop\n"
    "talking to it — not a request/response endpoint.\n"
    "\n"
    "The normal loop: `spawn_agent` with a goal (returns immediately, before the\n"
    "agent has done anything), then `get_agent` / `get_messages` to watch it work,\n"
    "`send_message` to steer or answer it, `terminate_agent` when it is done. Because\n"
    "agents work asynchronously, a transcript read right after a spawn is usually\n"
    "still empty; poll rather than assume failure.\n"
    "\n"
    "Every tool here acts on one cluster — the one "
)


def server_instructions(surface: Surface) -> str:
    """The common fleet instructions with this transport's cluster identity."""
    if surface == "stdio":
        return (
            _COMMON_INSTRUCTIONS + "this server was launched from.\nThere is no cluster argument."
        )
    if surface == "gateway":
        return (
            "\n" + _COMMON_INSTRUCTIONS + "this gateway belongs to.\nThere is no cluster argument."
        )
    raise ValueError(f"unknown MCP surface: {surface}")


def tool_description(name: str, surface: Surface) -> str:
    """Return the shared tool prose in the surface's existing wire layout."""
    description = _TOOL_DESCRIPTIONS[name]
    if surface == "stdio":
        return description
    if surface != "gateway":
        raise ValueError(f"unknown MCP surface: {surface}")
    if name in _GATEWAY_REWRAPS:
        for before, after in _GATEWAY_REWRAPS[name]:
            if description.count(before) != 1:
                raise ValueError(f"MCP description rewrap mismatch: {name}")
            description = description.replace(before, after, 1)
    if name == "send_message":
        if not description.endswith("\n        "):
            raise ValueError("MCP send_message description has an unexpected ending")
        description = description.removesuffix("\n        ") + _GATEWAY_SEND_MESSAGE_APPENDIX
    return description


def message_text(content: Any) -> str:
    """Flatten one message's content to text.

    A model message is either a plain string or a list of typed blocks
    (text / thinking / tool_use / image). Only the text blocks are readable
    prose; the rest is either rendered separately (tool calls, below) or not
    representable in a transcript read.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(cast(dict[str, Any], b)["text"])
            for b in cast(list[Any], content)
            if isinstance(b, dict) and cast(dict[str, Any], b).get("type") == "text"
        )
    return str(content)


def project_message(msg: dict[str, Any]) -> dict[str, Any]:
    """One transcript entry, reduced to role + text + the code the agent ran.

    Ava agents act by writing Python (`execute_code` is their only tool), so the
    code of a turn *is* what the agent did — dropping it would leave a reader
    seeing an agent that talks and never acts. Everything else in the raw
    message (ids, provider metadata, token accounting) is machinery.
    """
    projected: dict[str, Any] = {
        "role": msg["type"],
        "text": message_text(msg.get("content", "")),
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
