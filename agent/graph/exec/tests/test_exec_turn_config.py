"""The real execution child receives the configuration of its owning turn."""

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.runtime import Runtime

from agent.graph.exec._result import _ExecDone
from agent.graph.exec._stream import ExecOutputChunkPublisher
from agent.graph.exec.node import _run_agent_code, exec_node
from agent.state import AgentState
from ava.sdk_surface.process_context import process_clients
from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity
from base.agents.messages.kwargs import ExecStatus, read_ava_kwargs
from base.clock import Clock
from base.config import ConfigBoot, settings
from base.config.agent_pins import resolve_agent_config_pins
from base.db import Database
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.plugin_providers import build_model_catalog
from base.paths import workspace_dir_readonly
from tests.fixtures.configuration import snapshot_process_config


def _plugin(unit_home: Path) -> None:
    plugin = unit_home / "plugins" / "exec_config_probe"
    plugin.mkdir(parents=True)
    (plugin / "plugin.py").write_text("__description__ = 'Private configuration probe'\n")
    (plugin / "default_config.py").write_text(
        "from pydantic import BaseModel, Field\n"
        "from base.packages.plugins.extensions import PluginContributions\n"
        "class Config(BaseModel):\n"
        "    exec_probe_marker: str = Field(default='default-marker', json_schema_extra={'per_agent': True})\n"
        "def contribute():\n"
        "    return PluginContributions(config=Config)\n"
    )
    (unit_home / "plugins.json").write_text(
        json.dumps({"plugins": {"exec_config_probe": {"enabled": True}}})
    )


@pytest.mark.usefixtures("fake_cancel_event")
async def test_concurrent_turn_configs_reach_real_children_without_cross_talk(
    unit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    _plugin(unit_home)
    # The real child builds its own configuration owner from this isolated home.
    monkeypatch.setitem(os.environ, "AVA_HOME", str(unit_home))
    # Ambient carrier leftovers must not become an unbound child's pins.
    monkeypatch.setenv("AVA_AGENT_CONFIG_OVERLAY", json.dumps({"llm_model": "deepseek-v4-pro"}))
    monkeypatch.setenv(
        "AVA_AGENT_BIRTH_CONFIG",
        json.dumps(
            {"llm_stream_ttft_timeout_seconds": 99.0, "exec_probe_marker": "ambient-parent"}
        ),
    )
    code = (
        "import json, os\n"
        "from ava.sdk_surface.settings import agent_setting, plugins\n"
        "print('CONFIG=' + json.dumps([agent_setting('llm_model'), "
        "agent_setting('llm_stream_ttft_timeout_seconds'), "
        "plugins.exec_config_probe.exec_probe_marker, "
        "os.environ.get('AVA_AGENT_CONFIG_OVERLAY', 'GONE')]))\n"
    )

    async def execute(agent_id: int, slices: AgentSlices | None = None) -> list[object]:
        result, *_ = await _run_agent_code(
            AgentState(),
            AvaContext(
                agent=slices
                or AgentSlices.resolve(
                    default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
                ),
                db=database,
                bus=EventBus.from_settings(),
                clients=process_clients(
                    database=lambda: database,
                ),
                identity=AgentIdentity(agent_id=agent_id, owns_loop=True),
                catalog=build_model_catalog(),
                clock_factory=Clock.from_settings,
            ),
            agent_id,
            code,
            ExecOutputChunkPublisher(MagicMock(), agent_id, str(agent_id)),
        )
        assert isinstance(result, _ExecDone), result
        line = next(line for line in result.output.splitlines() if line.startswith("CONFIG="))
        return json.loads(line.removeprefix("CONFIG="))

    tasks: list[asyncio.Task[list[object]]] = []
    for agent_id, model, timeout, marker in (
        (424201, "deepseek-v4-pro", 3.0, "agent-a"),
        (424202, "deepseek-v4-flash-vision-exp", 7.0, "agent-b"),
    ):
        pins = resolve_agent_config_pins(
            {"llm_model": model},
            {"llm_model": "deepseek-v4-pro", "llm_stream_ttft_timeout_seconds": timeout},
        )
        slices = AgentSlices.resolve(
            pins,
            {"exec_config_probe": {"exec_probe_marker": marker}},
            default_reader=lambda domain, field: getattr(getattr(settings, domain), field),
        )
        tasks.append(asyncio.create_task(execute(agent_id, slices)))
    assert await asyncio.gather(*tasks) == [
        ["deepseek-v4-pro", 3.0, "agent-a", "GONE"],
        ["deepseek-v4-flash-vision-exp", 7.0, "agent-b", "GONE"],
    ]
    assert await execute(424203) == [
        settings.lm.llm_model,
        settings.lm.llm_stream_ttft_timeout_seconds,
        "default-marker",
        "GONE",
    ]


def _execution_context(
    owner: ConfigBoot,
    read: Callable[[str, str], Any],
    agent_id: int,
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    request: pytest.FixtureRequest,
) -> AvaContext:
    context = AvaContext(
        agent=AgentSlices.resolve(default_reader=read),
        db=database,
        bus=event_bus,
        clients=process_clients(config=owner, database=lambda: database),
        identity=AgentIdentity(agent_id=agent_id, owns_loop=True),
        event_publisher=MagicMock(),
        catalog=model_catalog,
        clock_factory=Clock.from_settings,
    )
    request.addfinalizer(context.clients.close)
    return context


async def _execution_message(context: AvaContext, code: str) -> ToolMessage:
    agent_id = context.require_identity().agent_id
    state = AgentState(
        messages=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "execute_code",
                        "args": {"code": code},
                        "id": "call",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    command = await exec_node(
        state, Runtime(context=context), {"configurable": {"thread_id": str(agent_id)}}
    )
    update = cast(dict[str, Any], command.update)
    assert update["halted"] is False
    message = update["messages"][0]
    assert isinstance(message, ToolMessage)
    return message


@pytest.mark.usefixtures("fake_cancel_event")
async def test_exec_shield_reads_each_owner_at_the_existing_timeout_points(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    request: pytest.FixtureRequest,
) -> None:
    first, second = snapshot_process_config(), snapshot_process_config()
    first.set_field("exec_node_timeout_seconds", 0.002)
    second.set_field("exec_node_timeout_seconds", 30)
    first_reads: list[float] = []
    second_reads: list[float] = []

    def read(owner: ConfigBoot, reads: list[float], domain: str, field: str) -> Any:
        value = getattr(getattr(owner.view, domain), field)
        if field == "exec_node_timeout_seconds":
            reads.append(value)
        return value

    contexts = [
        _execution_context(
            first,
            lambda domain, field: read(first, first_reads, domain, field),
            424204,
            database,
            event_bus,
            model_catalog,
            request,
        ),
        _execution_context(
            second,
            lambda domain, field: read(second, second_reads, domain, field),
            424205,
            database,
            event_bus,
            model_catalog,
            request,
        ),
    ]
    wait_for = asyncio.wait_for

    async def wait(awaitable: Awaitable[Any], timeout: float | None) -> Any:
        try:
            return await wait_for(awaitable, timeout)
        except TimeoutError:
            if timeout == 0.002:
                # The real framework wait has already requested and joined cancellation.
                # A changed owner must be read afresh at timeout logging and feedback.
                first.set_field("exec_node_timeout_seconds", 5)
            raise

    monkeypatch.setattr(asyncio, "wait_for", wait)
    timed_out, fast = await asyncio.gather(
        _execution_message(contexts[0], "import time; time.sleep(60)"),
        _execution_message(contexts[1], "print('done')"),
    )
    assert read_ava_kwargs(timed_out).get("ava_exec_status") == ExecStatus.TIMED_OUT
    assert "timeout after 5s" in timed_out.text
    assert read_ava_kwargs(fast).get("ava_exec_status") == ExecStatus.COMPLETED
    assert "done" in fast.text
    assert first_reads == [0.002, 5, 5]
    assert second_reads == [30]


@pytest.mark.usefixtures("fake_cancel_event")
async def test_crop_inputs_are_lazy_and_keep_two_live_readers_separate(
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    request: pytest.FixtureRequest,
) -> None:
    first, second = snapshot_process_config(), snapshot_process_config()
    for owner in (first, second):
        owner.set_field("exec_output_crop_head_lines", 1)
        owner.set_field("exec_output_crop_tail_lines", 1)
    first.set_field("exec_output_crop_after_lines", 2)
    second.set_field("exec_output_crop_after_lines", 7)
    reads: dict[str, list[int]] = {"first": [], "second": []}

    def read(owner: ConfigBoot, name: str, domain: str, field: str) -> Any:
        value = getattr(getattr(owner.view, domain), field)
        if field == "exec_output_crop_after_lines":
            reads[name].append(value)
        return value

    contexts = [
        _execution_context(
            first,
            lambda domain, field: read(first, "first", domain, field),
            424206,
            database,
            event_bus,
            model_catalog,
            request,
        ),
        _execution_context(
            second,
            lambda domain, field: read(second, "second", domain, field),
            424207,
            database,
            event_bus,
            model_catalog,
            request,
        ),
    ]
    assert reads == {"first": [], "second": []}
    code = "for index in range(6): print(f'line {index} ' + 'content ' * 40)"
    body = "".join(f"line {index} " + "content " * 40 + "\n" for index in range(6))
    first_result, second_result = await asyncio.gather(
        _execution_message(contexts[0], code),
        _execution_message(contexts[1], code),
    )
    assert "[output cropped:" in first_result.text
    assert "[output cropped:" not in second_result.text
    assert body in second_result.text
    archives = list((workspace_dir_readonly(424206) / ".exec_output").glob("crop_*.txt"))
    assert len(archives) == 1 and archives[0].read_text() == body
    assert str(archives[0]) in first_result.text
    first.set_field("exec_output_crop_after_lines", 9)
    updated, unchanged = await asyncio.gather(
        _execution_message(contexts[0], code),
        _execution_message(contexts[1], code),
    )
    assert "[output cropped:" not in updated.text and body in updated.text
    assert "[output cropped:" not in unchanged.text and body in unchanged.text
    assert reads == {"first": [2, 2, 9], "second": [7, 7]}
    assert list((workspace_dir_readonly(424206) / ".exec_output").glob("crop_*.txt")) == archives
    assert list((workspace_dir_readonly(424207) / ".exec_output").glob("crop_*.txt")) == []
