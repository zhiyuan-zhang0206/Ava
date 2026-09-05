"""Exec node drains pending attachments into a media message right after the output.

User ruling 2026-08-26 (Task #1668): the attach message must land immediately
after the ``execute_code`` output of the turn that registered the files — the
model sees them on its very next step of the same turn — instead of being
appended at the next turn boundary by the claim node.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph._context import AvaContext
from agent.graph._exec import _exec_node_impl
from agent.graph._exec_result import _ExecDone
from agent.state import AgentState, AttachState
from shared.message_kwargs import AvaMsgType
from tests.agent._fakes import make_fake_ops_pool

_CONFIG: RunnableConfig = {"configurable": {"thread_id": "7"}}

_TOOL_CALL_AIMESSAGE = AIMessage(
    content="",
    tool_calls=[
        {"name": "execute_code", "args": {"code": "x = 1"}, "id": "tc-1", "type": "tool_call"}
    ],
    response_metadata={"model_provider": "anthropic", "stop_reason": "tool_use"},
    usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
)


def _write_png(path: Path) -> None:
    from PIL import Image

    Image.new("RGB", (1, 1)).save(path)


def _make_runtime(model_name: str | None = None) -> Runtime[AvaContext]:
    """Minimal runtime with fake ops_pool + event_publisher (mirrors
    tests/agent/test_exec_node_timeout.py). A model_name selects the media
    capability gate for attachment packing (None = configured turn model)."""
    from types import SimpleNamespace
    from typing import cast

    from langchain_core.language_models.chat_models import BaseChatModel

    llm = None
    if model_name is not None:
        llm = cast("BaseChatModel", SimpleNamespace(model_name=model_name))
    ctx = AvaContext(
        ops_pool=make_fake_ops_pool(),
        llm=llm,
        event_publisher=MagicMock(),
    )
    return Runtime(context=ctx)


async def test_exec_drains_attachment_right_after_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image = tmp_path / "render.png"
    _write_png(image)

    async def _fake_run_agent_code(
        *args: object, **kwargs: object
    ) -> tuple[_ExecDone, dict[str, Any], int, list[Any], list[dict[str, str]] | None]:
        return (
            _ExecDone(output="exec output text", stream_cap=None),
            {},
            12,
            [],
            [{"path": str(image), "label": "brand"}],
        )

    monkeypatch.setattr("agent.graph._exec._run_agent_code", _fake_run_agent_code)

    state = AgentState(messages=[HumanMessage(content="hi"), _TOOL_CALL_AIMESSAGE], halted=False)
    result = await _exec_node_impl(
        state, _make_runtime(model_name="deepseek-v4-flash-vision-exp"), _CONFIG
    )

    update = result.update
    assert update is not None
    msgs = update.get("messages", [])
    assert len(msgs) == 2
    # exec output ToolMessage first, attach HumanMessage immediately after
    assert isinstance(msgs[0], ToolMessage)
    assert isinstance(msgs[1], HumanMessage)
    assert msgs[1].additional_kwargs["ava_msg_type"] == AvaMsgType.ATTACH.value
    # pending attachments are drained (cleared) in the same update
    assert update["attach"] == AttachState()  # type: ignore[index]
    # the attach message carries the caption + the native image block
    content = msgs[1].content
    assert isinstance(content, list)
    # Interleaved pack: [text(notice), text(caption line), image_url, ...] —
    # the file's own caption line sits directly before its media block.
    assert content[0]["type"] == "text"
    assert "Files attached during this turn" in content[0]["text"]  # pyright: ignore[reportUnknownIndexType]
    assert content[1]["type"] == "text"
    assert "render.png" in content[1]["text"]  # pyright: ignore[reportUnknownIndexType]
    assert content[2]["type"] == "image_url"


async def test_exec_without_attachments_appends_no_attach_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_run_agent_code(
        *args: object, **kwargs: object
    ) -> tuple[_ExecDone, dict[str, Any], int, list[Any], list[dict[str, str]] | None]:
        return (_ExecDone(output="exec output text", stream_cap=None), {}, 12, [], None)

    monkeypatch.setattr("agent.graph._exec._run_agent_code", _fake_run_agent_code)

    state = AgentState(messages=[HumanMessage(content="hi"), _TOOL_CALL_AIMESSAGE], halted=False)
    result = await _exec_node_impl(
        state, _make_runtime(model_name="deepseek-v4-flash-vision-exp"), _CONFIG
    )

    update = result.update
    assert update is not None
    msgs = update.get("messages", [])
    assert len(msgs) == 1
    assert isinstance(msgs[0], ToolMessage)
    # attach channel stays an empty state, no attach message appended
    assert update["attach"] == AttachState()  # type: ignore[index]
