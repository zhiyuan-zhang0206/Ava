"""Wire types for the browser-mcp line protocol (one JSON object per line).

Three processes speak this protocol and used to each hand-write its shape in a
docstring: the per-machine daemon (`mcp_daemon`, server), the per-agent bridge
(`mcp_wrapper`, client), and the healthcheck probe (`healthchecks.browser_mcp`).
These TypedDicts are its single definition so the three stay in lockstep — a
producer that builds a malformed envelope is caught at construction rather than
on the wire.

  Request:  {"id": 1, "method": "list_tools"}
            {"id": 2, "method": "call_tool", "tool": "click", "args": {...}}
            {"id": 0, "method": "ping"}
  Response: {"id": 1, "ok": true,  "result": ...}   # tool dicts / CallToolResult dump / None
            {"id": 2, "ok": false, "error": "message"}
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict


class Request(TypedDict):
    id: int
    method: str
    tool: NotRequired[str]  # call_tool only — the upstream tool name
    args: NotRequired[dict[str, Any]]  # call_tool only — the tool arguments


# Response is discriminated on `ok`, so `ok` and the field it guards are bound
# together: a success carries `result`, a failure carries `error`, and neither
# can be built without its partner. A reader that has checked `resp["ok"]` gets
# `result` as a present key, not a maybe-missing one.


class OkResponse(TypedDict):
    id: int | None  # echoes the request id
    ok: Literal[True]
    result: Any  # tool dicts / CallToolResult dump / None


class ErrResponse(TypedDict):
    id: int | None  # echoes the request id; None when the request never parsed
    ok: Literal[False]
    error: str


Response = OkResponse | ErrResponse
