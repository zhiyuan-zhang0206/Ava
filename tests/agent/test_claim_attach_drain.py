"""Turn-boundary conversion of pending attachments into one HumanMessage."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph._attach_drain import build_attach_drain
from agent.graph._claim import _claim_node_impl
from agent.graph._nodes import CLAIM
from agent.state import AttachEntry, AttachState, BaseAgentState
from shared.context import AvaContext
from shared.message_kwargs import AvaMsgType


def _write_png(path: Path) -> None:
    from PIL import Image

    Image.new("RGB", (1, 1)).save(path)


def _context(model_name: str) -> AvaContext:
    return AvaContext(
        ops_pool=MagicMock(),
        llm=cast("BaseChatModel", SimpleNamespace(model_name=model_name)),
        event_publisher=MagicMock(),
    )


def _blocks(message: HumanMessage) -> list[dict[str, Any]]:
    """Return a packed attachment message's native content blocks."""
    assert isinstance(message.content, list)  # pyright: ignore[reportUnknownMemberType]
    return cast("list[dict[str, Any]]", message.content)  # pyright: ignore[reportUnknownMemberType]


def test_drain_builds_one_native_image_message(tmp_path: Path) -> None:
    image = tmp_path / "render.png"
    _write_png(image)
    state = BaseAgentState(
        attach=AttachState(pending=[AttachEntry(path=str(image.resolve()), label="after fix")])
    )

    drain = build_attach_drain(state, _context("deepseek-v4-flash-vision-exp"))

    assert drain is not None
    assert drain["attach"] == AttachState()
    message = drain["messages"][0]
    assert isinstance(message, HumanMessage)
    assert message.additional_kwargs["ava_msg_type"] == AvaMsgType.ATTACH.value  # pyright: ignore[reportUnknownMemberType]
    text, image_block = _blocks(message)
    assert text["type"] == "text"
    assert "after fix" in text["text"]
    assert image_block["type"] == "image_url"


def test_drain_keeps_text_only_models_informed(tmp_path: Path) -> None:
    image = tmp_path / "render.png"
    _write_png(image)
    state = BaseAgentState(attach=AttachState(pending=[AttachEntry(path=str(image), label=None)]))

    drain = build_attach_drain(state, _context("deepseek-v4-pro"))

    assert drain is not None
    message = drain["messages"][0]
    assert isinstance(message, HumanMessage)
    blocks = _blocks(message)
    assert len(blocks) == 1
    assert "your model cannot receive image" in blocks[0]["text"]


def test_drain_is_noop_without_pending_attachments() -> None:
    assert build_attach_drain(BaseAgentState(), _context("deepseek-v4-pro")) is None


async def test_claim_drains_before_turn_boundary_wait(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image = tmp_path / "render.png"
    _write_png(image)
    state = BaseAgentState(
        halted=True,
        attach=AttachState(pending=[AttachEntry(path=str(image), label=None)]),
    )
    runtime = Runtime(context=_context("deepseek-v4-flash-vision-exp"))
    config: RunnableConfig = {"configurable": {"thread_id": "42"}}

    monkeypatch.setattr("agent.graph._claim.leave_starting_state", AsyncMock())
    monkeypatch.setattr("agent.graph._claim.claim_inbound_batch", AsyncMock(return_value=[]))

    command = await _claim_node_impl(state, runtime, config)

    assert command.goto == CLAIM
    assert command.update is not None
    assert command.update["attach"] == AttachState()  # type: ignore[index]


async def test_claim_does_not_drain_mid_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image = tmp_path / "render.png"
    _write_png(image)
    state = BaseAgentState(
        messages=[HumanMessage(content="continue working")],
        attach=AttachState(pending=[AttachEntry(path=str(image), label=None)]),
    )
    runtime = Runtime(context=_context("deepseek-v4-flash-vision-exp"))
    config: RunnableConfig = {"configurable": {"thread_id": "42"}}

    monkeypatch.setattr("agent.graph._claim.leave_starting_state", AsyncMock())
    monkeypatch.setattr("agent.graph._claim.claim_inbound_batch", AsyncMock(return_value=[]))

    command = await _claim_node_impl(state, runtime, config)

    assert command.goto == "before_llm"
    assert command.update is not None
    assert "attach" not in command.update  # type: ignore[operator]
