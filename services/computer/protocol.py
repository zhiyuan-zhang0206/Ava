"""Wire types for the computer-mcp line protocol (one JSON object per line).

Mirrors `services/browser/protocol.py`: the per-agent bridges and the MCP
daemon's direct dial speak this over the `computer-mcp` Unix socket; the daemon
serves it. `agent_id` rides on every request so the daemon can gate, quota, and
audit per agent — the bridge stamps it from the calling agent's identity.

  Request:  {"id": 1, "method": "ping"}
            {"id": 2, "method": "call_tool", "tool": "click", "args": {...}, "agent_id": 42}
  Response: {"id": 1, "ok": true,  "result": {...}}
            {"id": 2, "ok": false, "error": "message"}
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict


class Request(TypedDict):
    id: int
    method: str
    tool: NotRequired[str | None]  # call_tool only
    args: NotRequired[dict[str, Any] | None]  # call_tool only
    agent_id: NotRequired[int | None]  # the calling agent's identity, stamped by the bridge


class OkResponse(TypedDict):
    id: int | None  # echoes the request id; None when the request never parsed
    ok: Literal[True]
    result: Any  # tool dicts / tool result / "pong"


class ErrResponse(TypedDict):
    id: int | None  # echoes the request id; None when the request never parsed
    ok: Literal[False]
    error: str


Response = OkResponse | ErrResponse
