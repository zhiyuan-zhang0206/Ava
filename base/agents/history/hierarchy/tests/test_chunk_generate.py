# pyright: reportUnknownMemberType = warning
# pyright: reportUnknownArgumentType = warning
# pyright: reportUnknownLambdaType = warning
# pyright: reportUnknownVariableType = warning
"""The chunk instruction and the agent-shaped, grouping chunk request."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.exceptions import ModelAPIError, ModelInvalidRequestError
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from base.agents.history.hierarchy.chunk_generate import (
    build_catalog,
    build_chunk_instruction,
    generate_chunk,
)
from base.agents.history.hierarchy.chunks import ChunkCall
from base.agents.history.hierarchy.generate import GenerateError
from base.agents.history.hierarchy.leaf_groups import UnitGroup
from base.agents.history.hierarchy.units import divide_units
from base.config import settings


@pytest.fixture(autouse=True)
def _cluster_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.general, "timezone", "Asia/Shanghai")
    monkeypatch.setattr(settings.general, "message_timestamp_weekday", False)


def _inbound(text: str, at: str | None = "2026-10-05T03:04:05+00:00") -> HumanMessage:
    kwargs: dict[str, str] = {"ava_msg_type": "inbound", "ava_source": "user"}
    if at is not None:
        kwargs["ava_created_at"] = at
    return HumanMessage(content=text, additional_kwargs=kwargs)


def _turn(code: str, n: int, result: str = "ok", **result_kwargs: Any) -> list[BaseMessage]:
    call = {"name": "execute_code", "args": {"code": code}, "id": f"tc{n}"}
    return [
        AIMessage(content="", tool_calls=[call]),
        ToolMessage(
            content=f"Code execution output [2026-10-05 Mon 11:04:05]:\n\n{result}",
            tool_call_id=f"tc{n}",
            additional_kwargs={"ava_msg_type": "exec_output", **result_kwargs},
        ),
    ]


def _chunk() -> list[BaseMessage]:
    return [_inbound("fix the flaky test"), *_turn("ls -la", 1), *_turn("pytest -x", 2)]


def _instruction(chunk: list[BaseMessage] | None = None, **kw: bool) -> str:
    chunk = chunk or _chunk()
    return build_chunk_instruction(chunk, divide_units(chunk), **kw)


def _think_turn(think: str, code: str, n: int, result: str = "ok", **kw: Any) -> list[BaseMessage]:
    call = {"name": "execute_code", "args": {"code": code}, "id": f"tc{n}"}
    return [
        AIMessage(content=[{"type": "thinking", "thinking": think}], tool_calls=[call]),
        ToolMessage(
            content=f"Code execution output [x]:\n\n{result}",
            tool_call_id=f"tc{n}",
            additional_kwargs={"ava_msg_type": "exec_output", **kw},
        ),
    ]


def test_catalog_lines_are_type_content_and_a_work_line_has_its_three_parts() -> None:
    long = "x" * 300
    chunk = [
        _inbound(f"fix the flaky test {long}"),
        *_think_turn("plan " + "p" * 100, "ls  -la\n  -h", 1, "total 4\n  drwx" + "d" * 100),
    ]
    lines = build_catalog(chunk, divide_units(chunk)).splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("[1] human message: fix the flaky test xxx")
    assert len(lines[0]) == len("[1] human message: ") + 100  # content is cut at 100 characters
    assert lines[1] == (
        f"[2] work: reasoning\u300cplan {'p' * 55}\u300d | call\u300cls -la -h\u300d | "
        f"output\u300ctotal 4 drwx{'d' * 48}\u300d"  # each part is cut at 60 characters
    )


def test_a_work_line_leaves_out_the_parts_the_unit_does_not_have() -> None:
    no_think = _turn("pytest", 1, "passed")  # no reasoning
    no_output = [
        AIMessage(
            content="", tool_calls=[{"name": "execute_code", "args": {"code": "sleep"}, "id": "t"}]
        )
    ]
    only_thinking = [AIMessage(content=[{"type": "thinking", "thinking": "just thinking"}])]
    empty = _turn("d", 4, "")  # an empty output is left out
    chunk: list[BaseMessage] = [*no_think, *no_output, *only_thinking, *empty]
    lines = build_catalog(chunk, divide_units(chunk)).splitlines()
    assert lines == [
        "[1] work: call\u300cpytest\u300d | output\u300cpassed\u300d",
        "[2] work: call\u300csleep\u300d",
        "[3] work: reasoning\u300cjust thinking\u300d",
        "[4] work: call\u300cd\u300d",
    ]


def test_a_work_lines_output_part_is_the_start_of_the_output_whatever_its_status() -> None:
    chunk = [
        *_turn("a", 1, "boom", ava_exit_code=2),
        *_turn("b", 2, "x", ava_timed_out=True),
        *_turn("e", 5, "Traceback (most recent call last):\n  File x", ava_exit_code=1),
    ]
    lines = build_catalog(chunk, divide_units(chunk)).splitlines()
    assert lines == [
        "[1] work: call\u300ca\u300d | output\u300cboom\u300d",
        "[2] work: call\u300cb\u300d | output\u300cx\u300d",
        "[3] work: call\u300ce\u300d | output\u300cTraceback (most recent call last): File x\u300d",
    ]


def test_a_work_lines_call_part_skips_import_lines_and_keeps_all_import_code_whole() -> None:
    code = "import ava, os\nfrom pathlib import Path\n\n# check the queue\nprint(ava.shell.run('gh pr list'))"
    chunk = [_inbound("go"), *_turn(code, 1), *_turn("import os\nimport sys", 2)]
    lines = build_catalog(chunk, divide_units(chunk)).splitlines()
    assert (
        lines[1]
        == "[2] work: call\u300c# check the queue print(ava.shell.run('gh pr list'))\u300d | output\u300cok\u300d"
    )
    assert lines[2] == "[3] work: call\u300cimport os import sys\u300d | output\u300cok\u300d"


def test_the_turns_text_is_its_own_line_before_its_work_line() -> None:
    call = {"name": "execute_code", "args": {"code": "ls"}, "id": "t"}
    turn = AIMessage(
        content=[{"type": "thinking", "thinking": "plan it"}, {"type": "text", "text": "looking"}],
        tool_calls=[call],
    )
    result = ToolMessage(
        content="Code execution output [x]:\n\nfiles",
        tool_call_id="t",
        additional_kwargs={"ava_msg_type": "exec_output"},
    )
    chunk: list[BaseMessage] = [turn, result]
    assert build_catalog(chunk, divide_units(chunk)).splitlines() == [
        "[1] agent text: looking",
        "[2] work: reasoning\u300cplan it\u300d | call\u300cls\u300d | output\u300cfiles\u300d",
    ]


def test_inbound_lines_carry_the_sender_as_their_type_and_drop_the_sender_and_time_line() -> None:
    def inbound(source: str | None, text: str) -> HumanMessage:
        kwargs = {"ava_msg_type": "inbound", "ava_created_at": "2026-10-05T03:04:05+00:00"}
        if source:
            kwargs["ava_source"] = source
        return HumanMessage(content=text, additional_kwargs=kwargs)

    chunk = [
        inbound("agent:3230", "Agent 3230 [2026-10-05 Mon 11:04:05]:\n\nhello there"),
        inbound("user", "[2026-10-05 Mon 11:04:06]\n\n  fix it\n  now"),
        inbound("watcher:1955", "Watcher (id 1955) [2026-10-05 Mon 11:04:07]:\n\nwatch fired"),
        inbound("shell:1940", "Shell session (id 1940) [2026-10-05 Mon 11:04:08]:\n\nexited 0"),
        inbound("system:notice-reply", "[system] Re: x"),
        inbound(None, "no source"),
    ]
    lines = build_catalog(chunk, divide_units(chunk)).splitlines()
    assert lines == [
        "[1] agent 3230 message: hello there",
        "[2] human message: fix it now",
        "[3] watcher 1955: watch fired",
        "[4] shell 1940: exited 0",
        "[5] system notice-reply: [system] Re: x",
        "[6] human message: no source",
    ]


def _note(kind: str | None, tag: str | None = None) -> HumanMessage:
    kwargs: dict[str, str] = {} if kind is None else {"ava_msg_type": kind}
    if tag:
        kwargs["ava_note_tag"] = tag
    return HumanMessage(content="a long framework text that is not shown", additional_kwargs=kwargs)


def test_framework_notes_show_a_short_label_by_type_not_their_text() -> None:
    chunk = [
        _note("system_note", "memory"),
        _note("system_note", "sdk_hint"),
        _note("system_note"),
        _note("compact_summary"),
        _note("attach"),
        _note(None),
        _inbound("real message"),
    ]
    lines = build_catalog(chunk, divide_units(chunk)).splitlines()
    assert lines == [
        "[1] (memory)",
        "[2] (sdk hint)",
        "[3] (system note)",
        "[4] (compact summary)",
        "[5] (attachment)",
        "[6] (system note)",
        "[7] human message: real message",
    ]


def test_instruction_separates_itself_asks_for_numbers_and_carries_the_catalog() -> None:
    text = _instruction()
    assert text.startswith(
        "The messages above are background; what follows is a separate task.\n\nDivide the"
    )
    for part in (
        "Divide the conversation part listed in the catalog below into consecutive groups",
        "The catalog is the complete, ordered list of that part: refer to units by their number; "
        "there is no need to match them against the messages above.",
        "The summaries describe only the listed units; there is no need to confirm what "
        "happened outside them.",
        "The part is made of units: each inbound message is a unit, each piece of text the agent "
        "outputs is a unit, and the agent's reasoning, tool calls and the results of those calls "
        "together form a unit.",
        "A catalog line starts with its unit's type: the sender of an inbound message (human "
        "message, agent N message, watcher N, ...), agent text, or work (the start of its "
        "reasoning, call and output, in that order).",
        "A line in parentheses is a framework-injected message: join it to a neighbouring group; "
        "it needs no summary of its own.",
        "say at a higher level what happened in the group, much shorter than the messages it covers",
        "Write in the same language as the conversation.",
        "keeping consecutive units about the same matter together, and summarize each group",
        "a node in a tree one level above the raw messages",
        '<group first="N" last="M">summary</group>',
        "Do not call any tool",
        "Catalog (one unit per line; a group is a run of whole units):\n"
        "[1] human message: fix the flaky test\n[2] work: call\u300cls -la\u300d | output\u300cok\u300d\n[3] ",
    ):
        assert part in text
    for absent in (
        "<open",
        "left open",
        "its matter",
        "context only",
        "starts at unit 1",
        "starting a new group",
        "faithfully",
        "5 to 30",
        "quote",
    ):
        assert absent not in text


def test_empty_chunk_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one"):
        build_chunk_instruction([], [])


class _Recorder:
    """A chat model double: records the bound tools and the request it receives."""

    def __init__(self, replies: list[AIMessage]) -> None:
        self.replies = replies
        self.requests: list[list[Any]] = []
        self.bound: list[Any] | None = None

    def bind_tools(self, tools: list[Any]) -> _Recorder:
        self.bound = tools
        return self

    def invoke(self, messages: list[Any]) -> AIMessage:
        self.requests.append(list(messages))
        return self.replies.pop(0)


_PREFIX = (SystemMessage(content="head"), *_chunk())
_ONE = '<group first="1" last="3">  everything  </group>'
_TWO = '<group first="1" last="2">look</group><group first="3" last="3">run</group>'


def _gen(llm: Any, **kw: Any) -> Any:
    return generate_chunk(llm, _PREFIX, 1, model="m", tools=["T"], **kw)


def test_request_is_prefix_plus_one_instruction_with_tools_bound() -> None:
    llm = _Recorder([AIMessage(content=_TWO)])
    out = _gen(llm)
    assert out.groups == [UnitGroup(0, 1, "look"), UnitGroup(2, 2, "run")]
    assert [u.kind for u in out.units] == ["inbound", "work", "work"]
    assert llm.bound == ["T"]
    request = llm.requests[0]
    assert request[: len(_PREFIX)] == list(_PREFIX)  # byte-identical prefix, head in-band
    assert len(request) == len(_PREFIX) + 1 and isinstance(request[-1], HumanMessage)
    assert "[1] human message: fix the flaky test" in request[-1].content


def test_an_unclosed_group_is_refused_and_corrected() -> None:
    llm = _Recorder(
        [
            AIMessage(
                content='<group first="1" last="2">look<group first="3" last="3">run</group>'
            ),
            AIMessage(content=_TWO),
        ]
    )
    out = _gen(llm, corrections=1)
    assert out.groups[-1] == UnitGroup(2, 2, "run")  # the corrected reply tiles the catalog
    assert "opens 2 <group> tags but holds 1" in llm.requests[1][-1].content


def test_tool_call_reply_is_refused_and_retried_then_answered() -> None:
    call = AIMessage(
        content="", tool_calls=[{"name": "execute_code", "id": "c1", "args": {"code": "1"}}]
    )
    llm = _Recorder([call, AIMessage(content=_ONE)])
    out = _gen(llm)
    assert out.groups == [UnitGroup(0, 2, "everything")]
    retry = llm.requests[1]
    assert retry[-1].type == "tool" and "unavailable" in retry[-1].content


def test_endless_tool_calls_fail_after_the_refusal_rounds() -> None:
    call = AIMessage(
        content="", tool_calls=[{"name": "execute_code", "id": "c", "args": {"code": "1"}}]
    )
    with pytest.raises(GenerateError, match="kept calling tools"):
        _gen(_Recorder([call] * 10))


def test_a_refused_reply_is_resent_alone_in_the_same_conversation() -> None:
    bad = '<group first="1" last="2">a</group><group first="3" last="9">b</group>'
    llm = _Recorder([AIMessage(content=bad), AIMessage(content=_TWO)])
    seen: list[Any] = []
    out = _gen(llm, corrections=2, on_call=seen.append)
    assert out.groups == [UnitGroup(0, 1, "look"), UnitGroup(2, 2, "run")]
    second = llm.requests[1]
    assert (
        second[: len(_PREFIX) + 1] == llm.requests[0]
    )  # prefix + instruction untouched: cache hit
    assert second[-2].content == bad  # the model's own reply, then the correction
    assert "last 9 is not in the catalog (units 1 to 3)" in second[-1].content
    assert "Reply again with all the groups" in second[-1].content
    assert [(c.kind, c.round) for c in seen] == [("leaf", 0), ("group-correction", 1)]
    assert "last 9" in seen[0].problem and seen[1].problem is None
    assert seen[1].instruction == second[-1].content


def test_corrections_are_bounded_and_the_call_then_fails_with_every_attempt_recorded() -> None:
    bad = "<groups>nonsense</groups>"
    llm = _Recorder([AIMessage(content=bad)] * 3)
    seen: list[Any] = []
    with pytest.raises(GenerateError, match=r"refused after 2 correction\(s\)"):
        _gen(llm, corrections=2, on_call=seen.append)
    assert len(llm.requests) == 3 and [c.round for c in seen] == [0, 1, 2]
    assert all(c.problem for c in seen)


def test_no_corrections_means_the_first_refusal_fails() -> None:
    llm = _Recorder([AIMessage(content="no envelope")])
    with pytest.raises(GenerateError, match=r"refused after 0 correction\(s\)"):
        _gen(llm)
    assert len(llm.requests) == 1


def test_a_provider_error_in_a_correction_fails_the_call_and_is_recorded() -> None:
    class Flaky(_Recorder):
        def invoke(self, messages: list[Any]) -> AIMessage:
            if self.replies:
                return super().invoke(messages)
            raise ModelAPIError("500")

    seen: list[Any] = []
    with pytest.raises(GenerateError):
        _gen(
            Flaky([AIMessage(content="no envelope")]),
            corrections=1,
            retry_attempts=0,
            on_call=seen.append,
        )
    assert [c.kind for c in seen] == ["leaf", "group-correction"] and seen[1].response is None


def test_every_provider_call_is_reported_including_the_failed_one() -> None:
    call = AIMessage(
        content="", tool_calls=[{"name": "execute_code", "id": "c1", "args": {"code": "1"}}]
    )
    llm = _Recorder([call, AIMessage(content=[{"type": "text", "text": _ONE}])])
    seen: list[Any] = []
    _gen(llm, on_call=seen.append)
    assert [c.round for c in seen] == [0, 1]
    assert seen[0].response is call and seen[0].error is None
    assert all(c.prefix_len == len(_PREFIX) and c.start_offset == 1 for c in seen)
    assert "Catalog (one unit" in seen[0].instruction

    class Boom(_Recorder):
        def invoke(self, messages: list[Any]) -> AIMessage:
            raise ModelInvalidRequestError("400 bad request")

    failed: list[Any] = []
    with pytest.raises(GenerateError):
        _gen(Boom([]), retry_attempts=0, on_call=failed.append)
    assert len(failed) == 1 and failed[0].response is None and "400" in failed[0].error


@pytest.mark.parametrize("error", [TypeError("bad code"), ValueError("bad input")])
def test_unknown_chunk_invocation_error_is_recorded_once_and_preserved(error: Exception) -> None:
    class Broken(_Recorder):
        def invoke(self, messages: list[Any]) -> AIMessage:
            self.requests.append(list(messages))
            raise error

    llm = Broken([])
    calls: list[ChunkCall] = []
    with pytest.raises(type(error)) as raised:
        _gen(llm, corrections=2, retry_attempts=2, on_call=calls.append)
    assert raised.value is error
    assert len(llm.requests) == len(calls) == 1
    assert calls[0].response is None and calls[0].error == str(error)


def test_a_chunk_is_numbered_from_its_own_first_unit() -> None:
    # A chunk that starts at the second work turn: its catalog numbers from 1 there.
    llm = _Recorder([AIMessage(content='<group first="1" last="1">a</group>')])
    out = generate_chunk(llm, _PREFIX, 4, model="m", tools=["T"])
    catalog = llm.requests[0][-1].content.split("whole units):\n")[1]
    assert catalog.startswith("[1] ") and "pytest -x" in catalog and "[2]" not in catalog
    assert [u.i0 for u in out.units] == [0]
    llm = _Recorder(
        [
            AIMessage(
                content='<group first="1" last="1">a</group><group first="2" last="2">b</group>'
            )
        ]
    )
    with pytest.raises(GenerateError, match=r"first 2 is not in the catalog \(units 1 to 1\)"):
        generate_chunk(llm, _PREFIX, 4, model="m", tools=["T"])
