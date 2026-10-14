"""Public scenario recording contracts for async model calls and JSONL readers."""

import json
import os
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from tests.e2e.fakes.scenario_recording import (
    RecordingModel,
    exec_call,
    model_inputs,
    reset_record,
    say,
    scratch_root,
)
from tests.e2e.fakes.scripted_model import ScriptExhaustedError


async def test_async_calls_record_inputs_in_order_and_filter_by_agent() -> None:
    reset_record()
    first = RecordingModel(agent_id=17, script=(say("reply"),))
    second = RecordingModel(agent_id=23, script=(exec_call(2, "x = 1"),))
    prompt = HumanMessage(
        content=[{"type": "text", "text": "one"}, {"type": "text", "text": "two"}],
        additional_kwargs={"ava_note_tag": "summary"},
    )
    assert (await first.ainvoke([prompt])).content == "reply"
    chunks = [chunk async for chunk in second.astream([exec_call(1, "x = 0")])]
    assert chunks[0].tool_calls[0]["args"] == {"code": "x = 1"}
    assert first.cursor == second.cursor == 1
    expected: list[dict[str, Any]] = [
        {"type": "human", "text": "one\ntwo", "tag": "summary", "tool_calls": []}
    ]
    assert model_inputs(17) == [expected]
    assert model_inputs(23)[0][0]["tool_calls"] == ["execute_code"]
    assert model_inputs() == model_inputs(17) + model_inputs(23)
    entries = [
        json.loads(line)
        for line in (scratch_root("record") / "model_inputs.jsonl").read_text().splitlines()
    ]
    assert [entry["agent_id"] for entry in entries] == [17, 23]
    assert all(entry["pid"] == os.getpid() for entry in entries)
    reset_record()
    reset_record()
    assert model_inputs() == []


async def test_a_torn_tail_is_skipped_until_the_writer_terminates_it() -> None:
    """The record is appended by the agent process while the test process polls
    it, so a read can land mid-append. A line without its terminating newline is
    a call still being written: it is skipped instead of raising, and the next
    poll sees it whole once the writer terminates the line (the JSONDecodeError
    class observed in CI run 38497949470; issue #5765)."""
    reset_record()
    model = RecordingModel(agent_id=31, script=(say("first"), say("second")))
    await model.ainvoke([HumanMessage(content="first call")])

    path = scratch_root("record") / "model_inputs.jsonl"
    assert [call[0]["text"] for call in model_inputs(31)] == ["first call"]

    # A read racing the next append observes only a prefix of its line.
    rendered = json.dumps(
        {"agent_id": 31, "pid": os.getpid(), "messages": [{"type": "human", "text": "second call"}]}
    )
    half = len(rendered) // 2
    with path.open("a") as stream:
        stream.write(rendered[:half])
    assert [call[0]["text"] for call in model_inputs(31)] == ["first call"]

    with path.open("a") as stream:
        stream.write(rendered[half:] + "\n")
    assert [call[0]["text"] for call in model_inputs(31)] == ["first call", "second call"]
    reset_record()


def test_reader_line_boundaries_keep_terminated_corruption_loud() -> None:
    """The completeness boundary is the newline, so it is pinned at both ends:
    an empty or unterminated-only record reads as no calls, a terminated record
    reads whole, and a terminated line that fails to parse stays an error rather
    than passing as a missing record."""
    path = scratch_root("record") / "model_inputs.jsonl"
    reset_record()
    assert model_inputs() == []

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    assert model_inputs() == []

    path.write_text('{"agent_id": 1, "messages": [')
    assert model_inputs() == []

    path.write_text('{"agent_id": 1, "messages": [{"type": "human", "text": "x"}]}\n')
    assert [call[0]["text"] for call in model_inputs(1)] == ["x"]

    path.write_text('{"agent_id": 1, "messages": []}\n{"broken"\n')
    with pytest.raises(json.JSONDecodeError):
        model_inputs(1)
    reset_record()


async def test_exhausted_attempt_still_records_its_input() -> None:
    reset_record()
    model = RecordingModel(agent_id=None, script=())
    with pytest.raises(ScriptExhaustedError):
        await model.ainvoke([HumanMessage(content="attempt")])
    assert model.cursor == 0
    assert model_inputs()[0][0]["text"] == "attempt"
    reset_record()


def test_scratch_root_resolves_current_home_at_call_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AVA_HOME", str(tmp_path / "first"))
    first = scratch_root("record")
    monkeypatch.setenv("AVA_HOME", str(tmp_path / "second"))
    second = scratch_root("record")
    assert first != second
    assert first.name == "ava-e2e-record-first"
    assert second.name == "ava-e2e-record-second"


def test_script_helpers_preserve_tool_identity_and_usage() -> None:
    tool = exec_call(7, "value = 2")
    assert tool.tool_calls == [
        {"name": "execute_code", "args": {"code": "value = 2"}, "id": "call_7", "type": "tool_call"}
    ]
    reply = say("done")
    assert isinstance(reply, AIMessage)
    assert reply.content == "done"
    assert (
        tool.usage_metadata
        == reply.usage_metadata
        == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    )
