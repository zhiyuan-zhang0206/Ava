"""Exec-subprocess protocol — request/result envelopes and typed
(de)serialization, shared by the parent (`agent/graph/_exec_subprocess.py`)
and the child entry (`agent/exec_child.py`).

Two envelope files per run, both under `<exec_dir>/<agent_id>/` and chmod 0600 (the
snapshot carries the agent's full message history — same sensitivity as the
logs it shares the home with):

- request `<uuid>.json`: the code, the agent id, the timeout, and the typed
  state snapshot.
- result  `<uuid>.json`: the outcome kind, the plugin state-update delta, the
  security findings, attachments, the run's SDK-call tally, and — for a crash —
  the child-formatted traceback text.

On Windows the parent also creates a short-lived `<uuid>.job-ready.json` gate
after attaching the child to its Job Object. The child cannot enter user code
before that file exists. Request/gate leftovers are durable cleanup evidence
and are never age-pruned; normal resource settlement removes them exactly.

The envelope itself is plain JSON (cat-able for postmortem). The two typed
payloads — the state snapshot (parent -> child) and the state-update delta
(child -> parent) — ride as base64-wrapped `JsonPlusSerializer.dumps_typed`
blobs: that is the serializer the LangGraph checkpointer already uses with
`checkpoint_msgpack_allowlist`, so langchain messages, plugin pydantic models,
and `set` deltas round-trip exactly. Verified in tests (AIMessage
`usage_metadata` included; `convert_to_messages` would lose it — do not use
that here).

Security findings travel as plain JSON dicts (the parent re-validates them
into pydantic models).
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from shared.exec_process_domain import KILL_GRACE_S as KILL_GRACE_S
from shared.log import logger
from shared.runtime_incarnation import RuntimeIncarnation, current_incarnation

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

# Envelope size ceiling — a state snapshot cannot legitimately approach this;
# anything larger means an agent stuffed a giant object into the delta.
MAX_ENVELOPE_BYTES = 64 * 1024 * 1024

# Stale result files older than this are pruned when the parent allocates a new
# pair. Request/gate files are crash-stable resource evidence and are retained.
STALE_FILE_AGE_S = 3600.0

# Subdir name when no agent id is established (container/eval mode).
_NO_AGENT_DIRNAME = "_no_agent_"


def _serde() -> Any:
    """The typed-blob serializer — same allowlist as the LangGraph checkpointer.

    Imports deferred to the first actual (de)serialization: a request without a
    state snapshot never reaches here, so the child start path must not pay
    langgraph / `agent.state` at import (startup-path laziness, task #3585).
    The return type is Any on purpose: `JsonPlusSerializer` must stay out of
    module scope, and Pyright cannot resolve an annotation the module never
    imports. `dumps_typed` / `loads_typed` keep the precise public surface.
    """
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    from agent.state import checkpoint_msgpack_allowlist

    return JsonPlusSerializer(allowed_msgpack_modules=checkpoint_msgpack_allowlist())


def dumps_typed(obj: Any) -> tuple[str, bytes]:
    """Serialize `obj` as a typed blob (tag, bytes) — langchain messages,
    pydantic models, and sets round-trip exactly."""
    return _serde().dumps_typed(obj)


def loads_typed(data: tuple[str, bytes]) -> Any:
    """Reverse of `dumps_typed`."""
    return _serde().loads_typed(data)


@dataclass
class RequestPayload:
    """Decoded request envelope — what the parent hands the child.

    `state` is the typed-blob-decoded model dump, filled lazily by
    `materialize_state()`; until then the request's raw `(tag, blob)` rides
    `state_raw`. Deferring keeps the langgraph serde + `agent.state` off the
    child start for a snapshot the turn may never touch (task #3633 leg-2).
    Deliberately not frozen — the lazy memo writes back the one field.
    """

    code: str
    agent_id: int | None
    timeout_s: float
    state: dict[str, Any] | None  # typed-blob-decoded model dump (see materialize_state)
    incarnation: RuntimeIncarnation | None = None
    state_raw: tuple[str, bytes] | None = None

    def materialize_state(self) -> dict[str, Any] | None:
        """Decode the raw state blob once; None for a stateless request."""
        if self.state_raw is None:
            return self.state
        if self.state is None:
            decoded = loads_typed(self.state_raw)
            if not isinstance(decoded, dict):
                raise ValueError(
                    f"exec request state blob decoded to {type(decoded).__name__}, expected dict"
                )
            self.state = decoded
        return self.state


@dataclass
class ResultPayload:
    """Decoded result envelope — what the child hands back.

    `state_update` carries the raw `ava.state_update` delta (typed-blob
    decoded); `state_update_error` is set when the agent tampered with the
    slot (left it a non-dict) — the parent then raises the same TypeError the
    old in-process path raised. `findings` and `attachments` are plain JSON
    dicts drained from child-local buffers; `sdk_calls` is the run's real
    SDK-call tally."""

    kind: ResultKind
    lifecycle_type: str | None = None
    exc_type: str | None = None
    exc_msg: str | None = None
    full_traceback: str | None = None
    # True once the child reached the agent-authored code (set immediately
    # before exec); False on a boot-phase crash (before user code ran); None =
    # unknown (envelope without the field — pre-#2100 children). Lets the
    # parent tell "the code never ran" from "it ran and printed nothing".
    code_reached: bool | None = None
    state_update: dict[str, Any] | None = None
    state_update_error: str | None = None
    findings: list[dict[str, Any]] | None = None
    attachments: list[dict[str, Any]] | None = None
    # The run's SDK-call tally in `shared.sdk_telemetry.tally_entries` shape
    # (`[{"method": ..., "count": N}, ...]`); the exec node attaches it to the
    # exec_output ToolMessage as `additional_kwargs["sdk_calls"]`. None = the
    # code never ran (boot crash) — "ran, zero calls" is `[]`, a real zero.
    sdk_calls: list[dict[str, Any]] | None = None


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
    """Delete stale result envelopes and orphaned write temp files, never
    request/resource evidence.

    Successful settlement removes each request, result, and Windows job gate
    together. A leftover ``req-*`` file therefore represents uncertain cleanup
    after a killed parent and must survive age-based hygiene so an exclusive
    hosted boot can fail closed. The `.*.tmp` siblings are the atomic-write
    scratch files (_write_json): a live writer renames its own within
    milliseconds, so anything a full cutoff old has no writer left — the
    crash residue of task #3619 D-3. Bounded best-effort: two globs per spawn.
    """
    cutoff = time.time() - STALE_FILE_AGE_S
    for path in (*agent_dir.glob("*.json"), *agent_dir.glob(".*.tmp")):
        if path.name.startswith("req-"):
            continue
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
    started_at = time.perf_counter()
    envelope: dict[str, Any] = {
        "v": REQUEST_VERSION,
        "code": code,
        "agent_id": agent_id,
        "timeout_s": timeout_s,
    }
    incarnation = current_incarnation(agent_id) if agent_id is not None else None
    if incarnation is not None:
        envelope["incarnation"] = {
            "generation": str(incarnation.generation),
            "owner": str(incarnation.owner),
        }
    if state is not None:
        tag, blob = dumps_typed(state)
        envelope["state_tag"] = tag
        envelope["state_b64"] = base64.b64encode(blob).decode("ascii")
    _write_json(path, envelope)
    _log_envelope_transfer("request", "write", path, started_at)


def read_request(path: Path) -> RequestPayload:
    """Read a request envelope; fail-fast on version drift.

    The state blob stays raw (`state_raw`) until `materialize_state()` —
    a stateful child decodes it on first use, not at read (task #3633 leg-2).
    """
    started_at = time.perf_counter()
    envelope = _read_json(path)
    if envelope.get("v") != REQUEST_VERSION:
        raise ValueError(
            f"exec request envelope version {envelope.get('v')!r} != {REQUEST_VERSION} "
            f"(stale file or version skew between agent and exec child)"
        )
    state_raw = None
    if envelope.get("state_b64") is not None:
        # Raw (tag, blob) only — the decode is deferred to `materialize_state()`
        # so the langgraph serde + `agent.state` stay off the child start
        # (task #3633 leg-2).
        state_raw = (str(envelope["state_tag"]), base64.b64decode(envelope["state_b64"]))
    from uuid import UUID

    identity = envelope.get("incarnation")
    incarnation = None
    if identity is not None:
        if envelope["agent_id"] is None:
            raise ValueError("exec incarnation requires an agent id")
        incarnation = RuntimeIncarnation(
            int(envelope["agent_id"]), UUID(identity["generation"]), UUID(identity["owner"])
        )
    payload = RequestPayload(
        code=str(envelope["code"]),
        agent_id=envelope.get("agent_id"),
        timeout_s=float(envelope["timeout_s"]),
        state=None,
        incarnation=incarnation,
        state_raw=state_raw,
    )
    _log_envelope_transfer("request", "read", path, started_at)
    return payload


def write_result(path: Path, payload: ResultPayload) -> None:
    """Write the result envelope (0600)."""
    if payload.kind not in RESULT_KINDS:
        raise ValueError(f"unknown result kind {payload.kind!r}")
    started_at = time.perf_counter()
    envelope: dict[str, Any] = {
        "v": RESULT_VERSION,
        "kind": payload.kind,
        "lifecycle_type": payload.lifecycle_type,
        "exc_type": payload.exc_type,
        "exc_msg": payload.exc_msg,
        "full_traceback": payload.full_traceback,
        "code_reached": payload.code_reached,
        "state_update_error": payload.state_update_error,
        "findings": payload.findings,
        "attachments": payload.attachments,
        "sdk_calls": payload.sdk_calls,
    }
    if payload.state_update is not None:
        tag, blob = dumps_typed(payload.state_update)
        envelope["update_tag"] = tag
        envelope["update_b64"] = base64.b64encode(blob).decode("ascii")
    _write_json(path, envelope)
    _log_envelope_transfer("result", "write", path, started_at)


def read_result(path: Path) -> ResultPayload:
    """Read and decode a result envelope; fail-fast on version drift or an
    unknown kind."""
    started_at = time.perf_counter()
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
    code_reached = envelope.get("code_reached")
    payload = ResultPayload(
        kind=kind,
        lifecycle_type=envelope.get("lifecycle_type"),
        exc_type=envelope.get("exc_type"),
        exc_msg=envelope.get("exc_msg"),
        full_traceback=envelope.get("full_traceback"),
        code_reached=(code_reached if isinstance(code_reached, bool) else None),
        state_update=cast("dict[str, Any] | None", state_update),
        state_update_error=envelope.get("state_update_error"),
        findings=envelope.get("findings"),
        attachments=cast("list[dict[str, Any]] | None", envelope.get("attachments")),
        sdk_calls=cast("list[dict[str, Any]] | None", envelope.get("sdk_calls")),
    )
    _log_envelope_transfer("result", "read", path, started_at)
    return payload


def _log_envelope_transfer(
    envelope: Literal["request", "result"],
    op: Literal["read", "write"],
    path: Path,
    started_at: float,
) -> None:
    """Record an envelope transfer's final size and serialization cost."""
    size_bytes = path.stat().st_size
    serialize_ms = (time.perf_counter() - started_at) * 1000
    logger.info(
        "[exec envelope] {op} {envelope} size_bytes={size_bytes} serialize_ms={serialize_ms:.1f}",
        event="exec_envelope",
        envelope=envelope,
        op=op,
        size_bytes=size_bytes,
        serialize_ms=serialize_ms,
    )


def _write_json(path: Path, envelope: dict[str, Any]) -> None:
    """Write `envelope` as JSON, 0600, atomically replacing any file at `path`.

    The bytes land in a same-directory temp file first and become visible at
    `path` only through the final rename, so a writer killed mid-write can
    never leave a zero-byte or partial envelope behind (task #3619 D-3: a
    0-byte request envelope used to defer hosted boot recovery forever — see
    shared/exec_request_evidence.py). Owner-only from creation — `mkstemp`
    opens 0600 before any content lands, and the rename keeps that inode's
    mode, so no byte of message history ever sits at looser perms.
    """
    data = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    fd, raw_tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(raw_tmp)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)  # the rename is the only visible commit point
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _read_json(path: Path) -> dict[str, Any]:
    """Read + parse an envelope JSON, enforcing the size ceiling."""
    size_bytes = path.stat().st_size
    if size_bytes > MAX_ENVELOPE_BYTES:
        raise ValueError(
            f"exec envelope {path} is {size_bytes} bytes, over the {MAX_ENVELOPE_BYTES} "
            "ceiling — this state snapshot or result delta is over the exec envelope limit; "
            "compact the conversation"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"exec envelope {path} is {type(data).__name__}, expected object")
    return cast(dict[str, Any], data)
