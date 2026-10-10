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
    # unit_home sets this process's Settings; the real child imports Settings
    # afresh, so its bootstrap must receive the same isolated home in its env.
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
        "from base.config import settings\n"
        "from ava.sdk_surface.settings import plugins\n"
        "print('CONFIG=' + json.dumps([settings.lm.llm_model, "
        "settings.lm.llm_stream_ttft_timeout_seconds, "
        "plugins.exec_config_probe.exec_probe_marker, "
        "os.environ.get('AVA_AGENT_CONFIG_OVERLAY', 'GONE')]))\n"
    )

    async def execute(agent_id: int, slices: AgentSlices | None = None) -> list[object]:
        result, *_ = await _run_agent_code(
            AgentState(),
            AvaContext(
                agent=slices or AgentSlices.resolve(),
                db=database,
                bus=EventBus.from_settings(),
                clients=process_clients(
                    database=lambda: database,
                ),
                identity=AgentIdentity(agent_id=agent_id, owns_loop=True),
                catalog=build_model_catalog(),
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
        slices = AgentSlices.resolve(pins, {"exec_config_probe": {"exec_probe_marker": marker}})
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
