"""The real execution child receives the configuration of its owning turn."""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.graph.exec._result import _ExecDone
from agent.graph.exec._stream import ExecOutputChunkPublisher
from agent.graph.exec.node import _run_agent_code
from agent.state import AgentState
from ava.sdk_surface.process_context import process_clients
from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity
from base.clock import Clock
from base.config import settings
from base.config.agent_pins import resolve_agent_config_pins
from base.db import Database
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.plugin_providers import build_model_catalog


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


async def test_exec_shield_reads_each_owner_at_the_existing_timeout_points() -> None:
    from agent.graph.exec._result import _ExecTimedOut
    from agent.graph.exec.node import _exec_with_node_shield
    from base.config import ConfigBoot

    first, second = ConfigBoot(), ConfigBoot()
    first.set_field("exec_node_timeout_seconds", 0.002)
    second.set_field("exec_node_timeout_seconds", 30)
    first_reads: list[float] = []
    second_reads: list[float] = []

    def first_timeout() -> float:
        value = first.view.sandbox.exec_node_timeout_seconds
        first_reads.append(value)
        return value

    def second_timeout() -> float:
        value = second.view.sandbox.exec_node_timeout_seconds
        second_reads.append(value)
        return value

    async def stalled():
        first.set_field("exec_node_timeout_seconds", 5)
        await asyncio.Future()
        raise AssertionError("The stalled coroutine must be cancelled")

    async def completed():
        return _ExecDone(output="done"), None

    timed_out, fast = await asyncio.gather(
        _exec_with_node_shield(stalled(), 1, read_timeout=first_timeout),
        _exec_with_node_shield(completed(), 2, read_timeout=second_timeout),
    )
    assert isinstance(timed_out[0], _ExecTimedOut)
    assert "timeout after 5s" in timed_out[0].output
    assert isinstance(fast[0], _ExecDone)
    assert first_reads == [0.002, 5, 5]
    assert second_reads == [30]


def test_crop_inputs_are_lazy_and_keep_two_live_readers_separate() -> None:
    from agent.graph.exec._crop import CropInputs
    from base.config import ConfigBoot

    first, second = ConfigBoot(), ConfigBoot()
    first.set_field("exec_output_crop_after_lines", 2)
    second.set_field("exec_output_crop_after_lines", 7)
    reads: list[str] = []

    def first_read(field: str) -> int:
        reads.append(field)
        return getattr(first.view.sandbox, field)

    first_crop = CropInputs(first_read)
    second_crop = CropInputs(lambda field: getattr(second.view.sandbox, field))
    assert reads == []
    assert first_crop.exec_output_crop_after_lines == 2
    assert second_crop.exec_output_crop_after_lines == 7
    first.set_field("exec_output_crop_after_lines", 9)
    assert first_crop.exec_output_crop_after_lines == 9
    assert second_crop.exec_output_crop_after_lines == 7
    assert reads == ["exec_output_crop_after_lines"] * 2
