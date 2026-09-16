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
        attachments=[{"path": "/example/result.png", "label": "render"}],
        sdk_calls=[{"method": "files.read", "count": 3}],
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
    assert back.attachments == payload.attachments
    assert back.sdk_calls == payload.sdk_calls


def test_result_envelope_minimal(tmp_path: Path) -> None:
    path = make_result_path(tmp_path, agent_id=7)
    write_result(path, ResultPayload(kind="done"))
    back = read_result(path)
    assert back.kind == "done"
    assert back.state_update is None
    assert back.findings is None
    assert back.attachments is None


def test_result_envelope_without_attachments_reads_as_old_format(tmp_path: Path) -> None:
    path = make_result_path(tmp_path, agent_id=7)
    write_result(path, ResultPayload(kind="done"))
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["attachments"]
    path.write_text(json.dumps(raw), encoding="utf-8")

    assert read_result(path).attachments is None


def test_result_envelope_without_sdk_calls_reads_as_old_format(tmp_path: Path) -> None:
    path = make_result_path(tmp_path, agent_id=7)
    write_result(path, ResultPayload(kind="done"))
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["sdk_calls"]
    path.write_text(json.dumps(raw), encoding="utf-8")

    assert read_result(path).sdk_calls is None


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


def test_stale_request_evidence_retained_while_result_is_pruned(tmp_path: Path) -> None:
    path = make_request_path(tmp_path, agent_id=7)
    old_request = path.parent / "req-dead.json"
    old_request.write_text("{}", encoding="utf-8")
    old_gate = path.parent / "req-dead.job-ready.json"
    old_gate.write_text("{}", encoding="utf-8")
    old_result = path.parent / "res-dead.json"
    old_result.write_text("{}", encoding="utf-8")
    old_time = time.time() - STALE_FILE_AGE_S - 10
    import os

    for old in (old_request, old_gate, old_result):
        os.utime(old, (old_time, old_time))
    fresh = make_request_path(tmp_path, agent_id=7)
    assert old_request.exists()
    assert old_gate.exists()
    assert not old_result.exists()
    assert fresh.parent.is_dir()


def test_failed_envelope_write_leaves_nothing_at_the_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write failing before the rename never materializes at `path` (D-3)."""
    import agent.graph._exec_protocol as protocol

    path = make_request_path(tmp_path, agent_id=7)

    def refuse_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("replace refused")

    monkeypatch.setattr(protocol.os, "replace", refuse_replace)

    with pytest.raises(OSError, match="replace refused"):
        write_request(path, code="x = 1", agent_id=7, timeout_s=1.0, state=None)

    assert not path.exists()
    assert list(path.parent.glob(".*.tmp")) == []  # the scratch file is cleaned up


@pytest.mark.parametrize("kill_point", ["os.replace", "os.fsync"])
def test_writer_killed_before_commit_leaves_no_envelope_at_all(
    tmp_path: Path, kill_point: str
) -> None:
    """The rename is the only commit point: a writer killed just before it —
    or before the fsync that precedes it — never exposes a zero-byte or partial
    envelope, which is exactly what used to defer hosted boot recovery forever
    (task #3619 D-3)."""
    import subprocess
    import sys

    path = tmp_path / "req-crash.json"
    script = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "import agent.graph._exec_protocol as protocol\n"
        "def die(*_args, **_kwargs):\n"
        "    os._exit(9)\n"
        f"{kill_point} = die\n"
        "protocol.write_request(Path(sys.argv[1]), code='x=1', agent_id=7, "
        "timeout_s=1.0, state=None)\n"
    )
    completed = subprocess.run(  # noqa: S603 — our own venv python running a fixed in-test script
        [sys.executable, "-c", script, str(path)], check=False
    )

    assert completed.returncode == 9
    assert not path.exists()


def test_envelope_write_commits_the_full_bytes_and_a_second_write_replaces_them(
    tmp_path: Path,
) -> None:
    """The committed file is complete, owner-only, and leaves no scratch behind."""
    path = make_request_path(tmp_path, agent_id=7)
    write_request(path, code="x = 1", agent_id=7, timeout_s=1.0, state=None)
    first = path.read_text(encoding="utf-8")

    write_request(path, code="x = 2", agent_id=7, timeout_s=2.0, state=None)

    assert json.loads(path.read_text(encoding="utf-8"))["code"] == "x = 2"
    assert json.loads(first)["code"] == "x = 1"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.glob(".*.tmp")) == []


def test_orphaned_write_temp_files_are_swept(tmp_path: Path) -> None:
    """A crashed writer's scratch file is swept by a later allocation."""
    import os

    path = make_request_path(tmp_path, agent_id=7)
    agent_dir = path.parent
    orphan = agent_dir / ".req-dead.json.deadbeef.tmp"
    orphan.write_text("partial")
    live = agent_dir / ".req-live.json.cafef00d.tmp"
    live.write_text("partial")
    old_time = time.time() - STALE_FILE_AGE_S - 10
    os.utime(orphan, (old_time, old_time))

    make_request_path(tmp_path, agent_id=7)

    assert not orphan.exists()
    assert live.exists()


def test_size_ceiling_enforced(tmp_path: Path) -> None:
    path = make_request_path(tmp_path, agent_id=7)
    write_request(path, code="x = 1", agent_id=7, timeout_s=1.0, state=None)
    path.write_bytes(b"x" * (MAX_ENVELOPE_BYTES + 1))
    with pytest.raises(ValueError, match=r"ceiling.*compact the conversation"):
        read_request(path)


def test_no_agent_dirname(tmp_path: Path) -> None:
    path = make_request_path(tmp_path, agent_id=None)
    assert path.parent.name == "_no_agent_"
