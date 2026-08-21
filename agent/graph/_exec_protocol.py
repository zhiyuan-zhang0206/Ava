"""Exec-subprocess protocol — request/result envelopes and typed
(de)serialization, shared by the parent (`agent/graph/_exec_subprocess.py`)
and the child entry (`agent/exec_child.py`).

Two files per run, both under `<exec_dir>/<agent_id>/` and chmod 0600 (the
snapshot carries the agent's full message history — same sensitivity as the
logs it shares the home with):

- request `<uuid>.json`: the code, the agent id, the timeout, and the typed
  state snapshot.
- result  `<uuid>.json`: the outcome kind, the plugin state-update delta, the
  security findings, and — for a crash — the child-formatted traceback text.

The envelope itself is plain JSON (cat-able for postmortem). The two typed
payloads — the state snapshot (parent -> child) and the state-update delta
(child -> parent) — ride as base64-wrapped `JsonPlusSerializer.dumps_typed`
blobs: that is the serializer the LangGraph checkpointer already uses with
`checkpoint_msgpack_allowlist`, so langchain messages, plugin pydantic models,
and `set` deltas round-trip exactly. Verified in tests (AIMessage
`usage_metadata` included; `convert_to_messages` would lose it — do not use
that here).

Security findings travel as plain JSON dicts (`SecurityFindingEntry` is two
fields; the parent re-validates them into the pydantic model).
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from agent.state import checkpoint_msgpack_allowlist

# Envelope schema versions — bumped only on a breaking shape change.
REQUEST_VERSION = 1
RESULT_VERSION = 1

# Outcome kinds a child can report. The parent's own cancel/timeout flags stay
# authoritative; the child's kind is advisory except for `lifecycle` (only the
# child can know which lifecycle exception was raised).
ResultKind = Literal["done", "cancelled", "timed_out", "lifecycle", "crashed"]
RESULT_KINDS: frozenset[str] = frozenset({"done", "cancelled", "timed_out", "lifecycle", "crashed"})

# How long the parent waits for the child to exit after SIGINT/SIGTERM before
# escalating to SIGKILL. Shared here so the child's watchdog can sit beyond it.
KILL_GRACE_S = 2.0

# Envelope size ceiling — a state snapshot cannot legitimately approach this;
# anything larger means an agent stuffed a giant object into the delta.
MAX_ENVELOPE_BYTES = 64 * 1024 * 1024

# Stale request/result files older than this are pruned when the parent
# allocates a new pair (a killed parent can leave orphans behind).
STALE_FILE_AGE_S = 3600.0

# Subdir name when no agent id is established (container/eval mode).
_NO_AGENT_DIRNAME = "_no_agent_"


def _serde() -> JsonPlusSerializer:
    """The typed-blob serializer — same allowlist as the LangGraph checkpointer."""
    return JsonPlusSerializer(allowed_msgpack_modules=checkpoint_msgpack_allowlist())


def dumps_typed(obj: Any) -> tuple[str, bytes]:
    """Serialize `obj` as a typed blob (tag, bytes) — langchain messages,
    pydantic models, and sets round-trip exactly."""
    return _serde().dumps_typed(obj)


def loads_typed(data: tuple[str, bytes]) -> Any:
    """Reverse of `dumps_typed`."""
    return _serde().loads_typed(data)


@dataclass(frozen=True)
class RequestPayload:
    """Decoded request envelope — what the parent hands the child."""

    code: str
    agent_id: int | None
    timeout_s: float
    state: dict[str, Any] | None  # typed-blob-decoded model dump


@dataclass
class ResultPayload:
    """Decoded result envelope — what the child hands back.

    `state_update` carries the raw `ava.state_update` delta (typed-blob
    decoded); `state_update_error` is set when the agent tampered with the
    slot (left it a non-dict) — the parent then raises the same TypeError the
    old in-process path raised. `findings` are plain dicts of
    `ava.security.SecurityFindingEntry`."""

    kind: ResultKind
    lifecycle_type: str | None = None
    exc_type: str | None = None
    exc_msg: str | None = None
    full_traceback: str | None = None
    state_update: dict[str, Any] | None = None
    state_update_error: str | None = None
    findings: list[dict[str, Any]] | None = None


def make_request_path(exec_dir: Path, agent_id: int | None) -> Path:
    """Allocate a request path under `<exec_dir>/<agent_id>/`, pruning stale
    siblings; the file does not exist yet (caller writes it)."""
    agent_dir = _agent_dir(exec_dir, agent_id)
    _prune_stale(agent_dir)
    return agent_dir / f"req-{uuid.uuid4().hex}.json"


def make_result_path(exec_dir: Path, agent_id: int | None) -> Path:
    """Allocate a result path next to the request."""
    return _agent_dir(exec_dir, agent_id) / f"res-{uuid.uuid4().hex}.json"


def _agent_dir(exec_dir: Path, agent_id: int | None) -> Path:
    d = exec_dir / (str(agent_id) if agent_id is not None else _NO_AGENT_DIRNAME)
    d.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        d.chmod(0o700)  # owner-only, like the log dir
    return d


def _prune_stale(agent_dir: Path) -> None:
    """Delete request/result files older than `STALE_FILE_AGE_S` — leftovers
    from a killed parent. Bounded best-effort: a single glob per spawn."""
    cutoff = time.time() - STALE_FILE_AGE_S
    for path in agent_dir.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except FileNotFoundError:
            continue


def write_request(
    path: Path, *, code: str, agent_id: int | None, timeout_s: float, state: dict[str, Any] | None
) -> None:
    """Write the request envelope (0600). `state` is a model dump; it is
    serialized as a typed blob so langchain messages / plugin models survive."""
    envelope: dict[str, Any] = {
        "v": REQUEST_VERSION,
        "code": code,
        "agent_id": agent_id,
        "timeout_s": timeout_s,
    }
    if state is not None:
        tag, blob = dumps_typed(state)
        envelope["state_tag"] = tag
        envelope["state_b64"] = base64.b64encode(blob).decode("ascii")
    _write_json(path, envelope)


def read_request(path: Path) -> RequestPayload:
    """Read and decode a request envelope; fail-fast on version drift."""
    envelope = _read_json(path)
    if envelope.get("v") != REQUEST_VERSION:
        raise ValueError(
            f"exec request envelope version {envelope.get('v')!r} != {REQUEST_VERSION} "
            f"(stale file or version skew between agent and exec child)"
        )
    state = None
    if envelope.get("state_b64") is not None:
        state = loads_typed((str(envelope["state_tag"]), base64.b64decode(envelope["state_b64"])))
        if not isinstance(state, dict):
            raise ValueError(
                f"exec request state blob decoded to {type(state).__name__}, expected dict"
            )
    return RequestPayload(
        code=str(envelope["code"]),
        agent_id=envelope.get("agent_id"),
        timeout_s=float(envelope["timeout_s"]),
        state=cast("dict[str, Any] | None", state),
    )


def write_result(path: Path, payload: ResultPayload) -> None:
    """Write the result envelope (0600)."""
    if payload.kind not in RESULT_KINDS:
        raise ValueError(f"unknown result kind {payload.kind!r}")
    envelope: dict[str, Any] = {
        "v": RESULT_VERSION,
        "kind": payload.kind,
        "lifecycle_type": payload.lifecycle_type,
        "exc_type": payload.exc_type,
        "exc_msg": payload.exc_msg,
        "full_traceback": payload.full_traceback,
        "state_update_error": payload.state_update_error,
        "findings": payload.findings,
    }
    if payload.state_update is not None:
        tag, blob = dumps_typed(payload.state_update)
        envelope["update_tag"] = tag
        envelope["update_b64"] = base64.b64encode(blob).decode("ascii")
    _write_json(path, envelope)


def read_result(path: Path) -> ResultPayload:
    """Read and decode a result envelope; fail-fast on version drift or an
    unknown kind."""
    envelope = _read_json(path)
    if envelope.get("v") != RESULT_VERSION:
        raise ValueError(f"exec result envelope version {envelope.get('v')!r} != {RESULT_VERSION}")
    kind_raw = envelope.get("kind")
    if kind_raw not in RESULT_KINDS:
        raise ValueError(f"exec result envelope has unknown kind {kind_raw!r}")
    kind = cast(ResultKind, kind_raw)
    state_update = None
    if envelope.get("update_b64") is not None:
        state_update = loads_typed(
            (str(envelope["update_tag"]), base64.b64decode(envelope["update_b64"]))
        )
    if state_update is not None and not isinstance(state_update, dict):
        raise ValueError(
            f"exec result update blob decoded to {type(state_update).__name__}, expected dict"
        )
    return ResultPayload(
        kind=kind,
        lifecycle_type=envelope.get("lifecycle_type"),
        exc_type=envelope.get("exc_type"),
        exc_msg=envelope.get("exc_msg"),
        full_traceback=envelope.get("full_traceback"),
        state_update=cast("dict[str, Any] | None", state_update),
        state_update_error=envelope.get("state_update_error"),
        findings=envelope.get("findings"),
    )


def _write_json(path: Path, envelope: dict[str, Any]) -> None:
    """Write `envelope` as JSON, 0600, replacing any existing file at `path`."""
    data = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    # Owner-only create, tightened to exactly 0600 before any content lands:
    # the create mode passes through the umask, so chmod while the file is
    # still empty — no byte of message history ever sits at looser perms.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except BaseException:
        with contextlib.suppress(OSError):
            path.unlink()
        raise


def _read_json(path: Path) -> dict[str, Any]:
    """Read + parse an envelope JSON, enforcing the size ceiling."""
    if path.stat().st_size > MAX_ENVELOPE_BYTES:
        raise ValueError(
            f"exec envelope {path} is {path.stat().st_size} bytes, over the "
            f"{MAX_ENVELOPE_BYTES} ceiling"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"exec envelope {path} is {type(data).__name__}, expected object")
    return cast(dict[str, Any], data)
