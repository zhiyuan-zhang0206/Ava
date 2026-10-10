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
from agent.graph.claim.node import _claim_node_impl
from agent.nodes import CLAIM
from agent.state import AttachEntry, AttachState, BaseAgentState
from base.agents.context import AvaContext
from base.agents.messages.kwargs import AvaMsgType
from base.clock import Clock
from base.config import settings
from base.db import Database
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.plugin_providers import build_model_catalog


def _write_png(path: Path) -> None:
    from PIL import Image

    Image.new("RGB", (1, 1)).save(path)


def _context(model_name: str) -> AvaContext:
    return AvaContext(
        ops_pool=MagicMock(),
        llm=cast("BaseChatModel", SimpleNamespace(model_name=model_name)),
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(
            default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
        ),
        db=Database.from_settings(),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
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

    drain = build_attach_drain(state, _context("glm-5.3-flash"))

    assert drain is not None
    assert drain["attach"] == AttachState()
    message = drain["messages"][0]
    assert isinstance(message, HumanMessage)
    assert message.additional_kwargs["ava_msg_type"] == AvaMsgType.ATTACH.value  # pyright: ignore[reportUnknownMemberType]
    blocks = _blocks(message)
    # Interleaved pack: [text(notice), text(caption line), image_url, ...] —
    # the file's own caption line sits directly before its media block.
    assert [b["type"] for b in blocks] == ["text", "text", "image_url"]
    assert "Files attached during this turn" in blocks[0]["text"]
    assert "after fix" in blocks[1]["text"]
    assert blocks[2]["type"] == "image_url"


def test_drain_keeps_text_only_models_informed(tmp_path: Path) -> None:
    image = tmp_path / "render.png"
    _write_png(image)
    state = BaseAgentState(attach=AttachState(pending=[AttachEntry(path=str(image), label=None)]))

    drain = build_attach_drain(state, _context("deepseek-flash"))

    assert drain is not None
    message = drain["messages"][0]
    assert isinstance(message, HumanMessage)
    blocks = _blocks(message)
    # Skipped entry: notice + its caption line, both text blocks, no media block.
    assert [b["type"] for b in blocks] == ["text", "text"]
    assert "your model cannot receive image" in blocks[1]["text"]


def test_drain_is_noop_without_pending_attachments() -> None:
    assert build_attach_drain(BaseAgentState(), _context("deepseek-flash")) is None


async def test_claim_drains_before_turn_boundary_wait(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image = tmp_path / "render.png"
    _write_png(image)
    state = BaseAgentState(
        halted=True,
        attach=AttachState(pending=[AttachEntry(path=str(image), label=None)]),
    )
    runtime = Runtime(context=_context("glm-5.3-flash"))
    config: RunnableConfig = {"configurable": {"thread_id": "42"}}

    monkeypatch.setattr("agent.graph.claim.node.claim_inbound_batch", AsyncMock(return_value=[]))

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
    runtime = Runtime(context=_context("glm-5.3-flash"))
    config: RunnableConfig = {"configurable": {"thread_id": "42"}}

    monkeypatch.setattr("agent.graph.claim.node.claim_inbound_batch", AsyncMock(return_value=[]))

    command = await _claim_node_impl(state, runtime, config)

    assert command.goto == "before_llm"
    assert command.update is not None
    assert "attach" not in command.update  # type: ignore[operator]
