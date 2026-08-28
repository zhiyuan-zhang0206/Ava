"""Envelope + typed-blob protocol tests for the exec subprocess
(`agent/graph/_exec_protocol.py`).

The load-bearing assertions are the exact round-trips: the typed blob must
reconstruct langchain messages field-for-field (AIMessage `usage_metadata`
included — `convert_to_messages` loses it, which is why the protocol uses
`JsonPlusSerializer` with the checkpoint allowlist) and `set` deltas, since
the state snapshot (parent -> child) and the plugin state-update delta
(child -> parent) both ride it.
"""

from __future__ import annotations

import json
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.graph._exec_protocol import (
    MAX_ENVELOPE_BYTES,
    REQUEST_VERSION,
    RESULT_VERSION,
    STALE_FILE_AGE_S,
    ResultPayload,
    dumps_typed,
    loads_typed,
    make_request_path,
    make_result_path,
    read_request,
    read_result,
    write_request,
    write_result,
)


def _exec_envelope_events() -> list[dict[str, Any]]:
    """Read the durable telemetry mirror's exec-envelope rows."""
    from shared.paths import logs_dir

    path = logs_dir() / f"events-{datetime.now(UTC):%Y%m%d}.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("event_name") == "exec_envelope":
            events.append(row)
    return events


# ── typed blob: exact round-trip (the reason this codec was chosen) ──────


def test_typed_blob_aimessage_exact_round_trip() -> None:
    """AIMessage with tool_calls / usage_metadata / response_metadata must
    come back field-for-field identical — the alternative
    (`convert_to_messages`) silently relocates `usage_metadata` into
    `additional_kwargs`, which would corrupt the snapshot's message history."""
    msg = AIMessage(
        content="",
        tool_calls=[
            {"name": "execute_code", "args": {"code": "x = 1"}, "id": "tc-1", "type": "tool_call"}
        ],
        response_metadata={"model_provider": "anthropic", "stop_reason": "tool_use"},
        usage_metadata={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
    )
    back = loads_typed(dumps_typed(msg))
    assert type(back) is AIMessage
    assert back.model_dump() == msg.model_dump()


def test_typed_blob_tool_and_human_messages_exact_round_trip() -> None:
    """ToolMessage additional_kwargs (ava_exit_code) and a plain HumanMessage
    survive unchanged."""
    msgs = [
        ToolMessage(content="out", tool_call_id="tc-1", additional_kwargs={"ava_exit_code": 0}),
        HumanMessage(content="hi"),
    ]
    back = loads_typed(dumps_typed(msgs))
    assert [type(m) for m in back] == [type(m) for m in msgs]
    assert [m.model_dump() for m in back] == [m.model_dump() for m in msgs]


def test_typed_blob_set_round_trip() -> None:
    """A custom plugin reducer delta can be a `set` — the codec must keep the
    type (the LangGraph reducer would break on a list)."""
    back = loads_typed(dumps_typed({"seen": {"a", "b"}}))
    assert back == {"seen": {"a", "b"}}
    assert isinstance(back["seen"], set)


# ── request envelope ──────────────────────────────────────────────────────


def _make_state_dump() -> dict[str, Any]:
    msg = AIMessage(
        content="snapshot message",
        tool_calls=[
            {"name": "execute_code", "args": {"code": "pass"}, "id": "tc-1", "type": "tool_call"}
        ],
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    return {"messages": [msg], "halted": False}


def test_request_envelope_round_trip(tmp_path: Path) -> None:
    state = _make_state_dump()
    path = make_request_path(tmp_path, agent_id=7)
    write_request(path, code="print('hi')", agent_id=7, timeout_s=300.0, state=state)
    payload = read_request(path)
    assert payload.code == "print('hi')"
    assert payload.agent_id == 7
    assert payload.timeout_s == 300.0
    assert payload.state == state  # exact, typed (messages back as instances)


def test_request_envelope_transfers_emit_size_and_serialize_time(tmp_path: Path) -> None:
    """Request writes and reads record the final envelope size and their own
    serialization cost in the durable event stream."""
    from shared import telemetry
    from shared.log import _add_postgres_sink, logger

    events_before = len(_exec_envelope_events())
    sink_id = _add_postgres_sink(process="test-exec-envelope")
    try:
        path = make_request_path(tmp_path, agent_id=7)
        write_request(path, code="print('hi')", agent_id=7, timeout_s=1.0, state=_make_state_dump())
        read_request(path)

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            telemetry.flush()
            if len(_exec_envelope_events()) >= events_before + 2:
                break
            time.sleep(0.05)
    finally:
        logger.remove(sink_id)

    events = _exec_envelope_events()[events_before:]
    assert len(events) >= 2
    request_events = [event for event in events if event["attributes"]["envelope"] == "request"]
    by_op = {event["attributes"]["op"]: event["attributes"] for event in request_events}
    assert set(by_op) >= {"write", "read"}
    for attrs in by_op.values():
        assert attrs["size_bytes"] == path.stat().st_size
        assert isinstance(attrs["serialize_ms"], float)
        assert attrs["serialize_ms"] >= 0.0


def test_request_envelope_without_state(tmp_path: Path) -> None:
    path = make_request_path(tmp_path, agent_id=None)
    write_request(path, code="x = 1", agent_id=None, timeout_s=0.0, state=None)
    payload = read_request(path)
    assert payload.agent_id is None
    assert payload.state is None
    # no state keys in the raw JSON
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "state_b64" not in raw


def test_request_envelope_rejects_version_drift(tmp_path: Path) -> None:
    path = make_request_path(tmp_path, agent_id=7)
    write_request(path, code="x = 1", agent_id=7, timeout_s=1.0, state=None)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["v"] = REQUEST_VERSION + 99
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        read_request(path)


# ── result envelope ───────────────────────────────────────────────────────


def test_result_envelope_round_trip(tmp_path: Path) -> None:
    payload = ResultPayload(
        kind="crashed",
        exc_type="ValueError",
        exc_msg="boom",
        full_traceback="Traceback...",
        state_update={"messages": [HumanMessage(content="note")]},
        findings=[{"type": "security", "source": "file.read:x", "triggers": ["[system]"]}],
        pause_notes=[
            {
                "content": (
                    "Previous heartbeat pause window: 30m; this pause: 30m. "
                    "If the waited-for event has not changed, pausing for the "
                    "same or a shorter window than the previous pause violates "
                    "the backoff rule: pause windows must increase "
                    "(30m -> 2h -> 4h -> 8h -> 16h -> 24h)."
                )
            }
        ],
        attachments=[{"path": "/example/result.png", "label": "render"}],
    )
    path = make_result_path(tmp_path, agent_id=7)
    write_result(path, payload)
    back = read_result(path)
    assert back.kind == "crashed"
    assert back.exc_type == "ValueError"
    assert back.exc_msg == "boom"
    assert back.full_traceback == "Traceback..."
    assert back.state_update == payload.state_update  # messages back as instances
    assert back.findings == payload.findings
    assert back.pause_notes == payload.pause_notes
    assert back.attachments == payload.attachments


def test_result_envelope_minimal(tmp_path: Path) -> None:
    path = make_result_path(tmp_path, agent_id=7)
    write_result(path, ResultPayload(kind="done"))
    back = read_result(path)
    assert back.kind == "done"
    assert back.state_update is None
    assert back.findings is None
    assert back.pause_notes is None
    assert back.attachments is None


def test_result_envelope_without_attachments_reads_as_old_format(tmp_path: Path) -> None:
    path = make_result_path(tmp_path, agent_id=7)
    write_result(path, ResultPayload(kind="done"))
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["attachments"]
    path.write_text(json.dumps(raw), encoding="utf-8")

    assert read_result(path).attachments is None


def test_result_envelope_rejects_unknown_kind(tmp_path: Path) -> None:
    path = make_result_path(tmp_path, agent_id=7)
    write_result(path, ResultPayload(kind="done"))
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["kind"] = "exploded"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown kind"):
        read_result(path)


def test_result_envelope_rejects_version_drift(tmp_path: Path) -> None:
    path = make_result_path(tmp_path, agent_id=7)
    write_result(path, ResultPayload(kind="done"))
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["v"] = RESULT_VERSION + 99
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        read_result(path)


# ── file hygiene ──────────────────────────────────────────────────────────


def test_envelope_files_are_owner_only(tmp_path: Path) -> None:
    request = make_request_path(tmp_path, agent_id=7)
    write_request(request, code="x = 1", agent_id=7, timeout_s=1.0, state=None)
    result = make_result_path(tmp_path, agent_id=7)
    write_result(result, ResultPayload(kind="done"))
    for path in (request, result):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"{path.name} mode {oct(mode)} != 0600"
    agent_dir = request.parent
    assert stat.S_IMODE(agent_dir.stat().st_mode) == 0o700


def test_result_write_fails_on_unknown_kind(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown result kind"):
        write_result(make_result_path(tmp_path, agent_id=7), ResultPayload(kind="nope"))  # type: ignore[arg-type]


def test_stale_files_pruned_on_alloc(tmp_path: Path) -> None:
    path = make_request_path(tmp_path, agent_id=7)
    old = path.parent / "req-dead.json"
    old.write_text("{}", encoding="utf-8")
    old_time = time.time() - STALE_FILE_AGE_S - 10
    import os

    os.utime(old, (old_time, old_time))
    fresh = make_request_path(tmp_path, agent_id=7)
    assert not old.exists()
    assert fresh.parent.is_dir()


def test_size_ceiling_enforced(tmp_path: Path) -> None:
    path = make_request_path(tmp_path, agent_id=7)
    write_request(path, code="x = 1", agent_id=7, timeout_s=1.0, state=None)
    path.write_bytes(b"x" * (MAX_ENVELOPE_BYTES + 1))
    with pytest.raises(ValueError, match=r"ceiling.*compact the conversation"):
        read_request(path)


def test_no_agent_dirname(tmp_path: Path) -> None:
    path = make_request_path(tmp_path, agent_id=None)
    assert path.parent.name == "_no_agent_"
