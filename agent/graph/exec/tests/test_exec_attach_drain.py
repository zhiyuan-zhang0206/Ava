from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph.exec import node
from agent.graph.exec._result import ExecDone
from agent.graph.exec.node import _exec_node_impl
from agent.hooks.tests.fakes import make_fake_ops_pool
from agent.state import AgentState, AttachState
from base.agents.context import MeivaContext
from base.agents.messages.kwargs import MeivaMsgType
from base.clock import Clock
from base.config.tests.configuration import defaults
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.native_process.turn_identity import HostedTurnResources
from base.packages.plugin_providers import build_model_catalog
from meiva.tests.fixtures.pin_agent import hosted_resources as hosted_resources

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


def _make_runtime(
    hosted_resources: HostedTurnResources,
    model_name: str | None = None,
    *,
    database_gate: ProcessDbGate,
) -> Runtime[MeivaContext]:
    from types import SimpleNamespace
    from typing import cast

    from langchain_core.language_models.chat_models import BaseChatModel

    llm = None
    if model_name is not None:
        llm = cast("BaseChatModel", SimpleNamespace(model_name=model_name))
    ctx = MeivaContext(
        hosted_resources=hosted_resources,
        ops_pool=make_fake_ops_pool(),
        llm=llm,
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(default_reader=defaults()),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
    )
    return Runtime(context=ctx)


async def test_exec_drains_attachment_right_after_output(
    hosted_resources: HostedTurnResources,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database_gate: ProcessDbGate,
) -> None:
    image = tmp_path / "render.png"
    _write_png(image)

    async def _fake_run_agent_code(
        *args: object, **kwargs: object
    ) -> tuple[ExecDone, dict[str, Any], int, list[dict[str, str]] | None, list[Any] | None]:
        return (
            ExecDone(output="exec output text", stream_cap=None),
            {},
            12,
            [{"path": str(image), "label": "brand"}],
            [{"method": "files.read", "count": 3}],
        )

    monkeypatch.setattr(node, "run_agent_code", _fake_run_agent_code)

    state = AgentState(messages=[HumanMessage(content="hi"), _TOOL_CALL_AIMESSAGE], halted=False)
    result = await _exec_node_impl(
        state,
        _make_runtime(
            hosted_resources=hosted_resources,
            model_name="glm-5.3-flash",
            database_gate=database_gate,
        ),
        _CONFIG,
    )

    update = result.update
    assert update is not None
    msgs = update.get("messages", [])
    assert len(msgs) == 2
    assert isinstance(msgs[0], ToolMessage)
    assert msgs[0].additional_kwargs["sdk_calls"] == [{"method": "files.read", "count": 3}]
    assert isinstance(msgs[1], HumanMessage)
    assert msgs[1].additional_kwargs["meiva_msg_type"] == MeivaMsgType.ATTACH.value
    assert update["attach"] == AttachState()
    content = msgs[1].content
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert "Files attached during this turn" in content[0]["text"]
    assert content[1]["type"] == "text"
    assert "render.png" in content[1]["text"]
    assert content[2]["type"] == "image_url"


async def test_exec_without_attachments_appends_no_attach_message(
    hosted_resources: HostedTurnResources,
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
) -> None:
    async def _fake_run_agent_code(
        *args: object, **kwargs: object
    ) -> tuple[ExecDone, dict[str, Any], int, list[dict[str, str]] | None, list[Any] | None]:
        return (ExecDone(output="exec output text", stream_cap=None), {}, 12, None, None)

    monkeypatch.setattr(node, "run_agent_code", _fake_run_agent_code)

    state = AgentState(messages=[HumanMessage(content="hi"), _TOOL_CALL_AIMESSAGE], halted=False)
    result = await _exec_node_impl(
        state,
        _make_runtime(
            hosted_resources=hosted_resources,
            model_name="glm-5.3-flash",
            database_gate=database_gate,
        ),
        _CONFIG,
    )

    update = result.update
    assert update is not None
    msgs = update.get("messages", [])
    assert len(msgs) == 1
    assert isinstance(msgs[0], ToolMessage)
    assert "sdk_calls" not in msgs[0].additional_kwargs
    assert update["attach"] == AttachState()
