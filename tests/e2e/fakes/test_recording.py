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
