"""The K1 control-plane wire protocol (one JSON object per line).

The root supervisor speaks this protocol over a unix socket (the per-platform
transport is an OS-edge concern; a named pipe is the equivalent there and
lands with the platform adapter work). The two ends of this module are
transport-only: `server.py` binds the socket, `client.py` dials it, and both
validate shapes fail-fast so a drifting peer is rejected at the boundary
instead of being carried into the supervisor.

Wire shape — one UTF-8 line per message, newline-terminated:

    request   {"verb": "up", "name": "gateway"}
              {"verb": "status"}
    response  {"ok": true, "result": {...}}
              {"ok": false, "code": "unknown_unit", "error": "unknown unit 'x'"}
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from typing import NotRequired, TypedDict, cast

# One message is one line; the cap bounds how much a peer can make us buffer.
MAX_MESSAGE_BYTES = 64 * 1024

# The verbs that name a target unit; the others take no name.
_NAMED_VERBS = frozenset({"up", "down", "restart"})
_REQUEST_FIELDS = frozenset({"verb", "name"})
_RESPONSE_FIELDS = frozenset({"ok", "result", "error", "code"})


class Verb(StrEnum):
    """The K1 control verbs."""

    UP = "up"
    DOWN = "down"
    RESTART = "restart"
    STATUS = "status"
    UPGRADE = "upgrade"


class ErrorCode(StrEnum):
    """Stable failure codes; clients branch on these, not on message text."""

    INVALID_REQUEST = "invalid_request"
    UNKNOWN_VERB = "unknown_verb"
    UNKNOWN_UNIT = "unknown_unit"
    NOT_IMPLEMENTED = "not_implemented"
    INTERNAL = "internal"


class RequestPayload(TypedDict):
    """A validated request in wire form."""

    verb: str
    name: NotRequired[str]


class ResponsePayload(TypedDict):
    """A response in wire form; either `result` (ok) or `error` + `code`."""

    ok: bool
    result: NotRequired[object]
    error: NotRequired[str]
    code: NotRequired[str]


class ProtocolError(ValueError):
    """A wire message is malformed; the message is rejected, never guessed at."""


class UnknownVerbError(ProtocolError):
    """The message names a verb this protocol does not define."""


def encode(message: Mapping[str, object]) -> bytes:
    """One wire message as a single newline-terminated UTF-8 line."""
    line = json.dumps(message, ensure_ascii=True, separators=(",", ":"))
    return line.encode("utf-8") + b"\n"


def parse_request(raw: bytes) -> RequestPayload:
    """Validate one request line; every deviation raises ProtocolError."""
    document = _decode_object(raw)
    unknown = set(document) - _REQUEST_FIELDS
    if unknown:
        raise ProtocolError(f"unknown request field(s): {sorted(unknown)}")
    verb_raw = document.get("verb")
    if not isinstance(verb_raw, str):
        raise ProtocolError("request must carry a string 'verb'")
    name_raw = document.get("name")
    if verb_raw in _NAMED_VERBS:
        if not isinstance(name_raw, str) or not name_raw:
            raise ProtocolError(f"verb {verb_raw!r} requires a non-empty 'name'")
        named: RequestPayload = {"verb": verb_raw, "name": name_raw}
        return named
    try:
        Verb(verb_raw)
    except ValueError as exc:
        raise UnknownVerbError(f"unknown verb {verb_raw!r}") from exc
    if name_raw is not None:
        raise ProtocolError(f"verb {verb_raw!r} takes no 'name'")
    unnamed: RequestPayload = {"verb": verb_raw}
    return unnamed


def parse_response(raw: bytes) -> ResponsePayload:
    """Validate one response line; every deviation raises ProtocolError."""
    document = _decode_object(raw)
    unknown = set(document) - _RESPONSE_FIELDS
    if unknown:
        raise ProtocolError(f"unknown response field(s): {sorted(unknown)}")
    ok_raw = document.get("ok")
    if not isinstance(ok_raw, bool):
        raise ProtocolError("response must carry a boolean 'ok'")
    if ok_raw:
        accepted: ResponsePayload = {"ok": True, "result": document.get("result")}
        return accepted
    error_raw = document.get("error")
    if not isinstance(error_raw, str):
        raise ProtocolError("a failed response must carry a string 'error'")
    code_raw = document.get("code")
    try:
        code = ErrorCode(code_raw)
    except ValueError as exc:
        raise ProtocolError(f"failed response carries an unknown code {code_raw!r}") from exc
    rejected: ResponsePayload = {"ok": False, "error": error_raw, "code": code.value}
    return rejected


def ok_response(result: object) -> ResponsePayload:
    """A success response carrying `result`."""
    return {"ok": True, "result": result}


def error_response(code: ErrorCode, message: str) -> ResponsePayload:
    """A failure response carrying a stable code and a human message."""
    return {"ok": False, "code": code.value, "error": message}


def _decode_object(raw: bytes) -> dict[str, object]:
    """Decode one message into a JSON object; anything else raises ProtocolError."""
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"message is not UTF-8: {exc}") from exc
    try:
        value = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"message is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError("message must be a JSON object")
    return cast("dict[str, object]", value)
