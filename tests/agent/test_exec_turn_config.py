"""The real execution child receives the configuration of its owning turn."""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.graph._context import AvaContext
from agent.graph._exec import _run_agent_code
from agent.graph._exec_result import _ExecDone
from agent.graph._exec_stream import ExecOutputChunkPublisher
from agent.state import AgentState
from shared.config import settings
from shared.config.turn_view import bind_agent_config, resolve_agent_config_pins
from shared.plugin_config_view import bind_agent_plugin_config
from shared.turn_identity import bind_turn_identity


def _plugin(unit_home: Path) -> None:
    plugin = unit_home / "plugins" / "exec_config_probe"
    plugin.mkdir(parents=True)
    (plugin / "plugin.py").write_text(
        "__description__ = 'Private configuration probe'\nfrom . import default_config\n"
    )
    (plugin / "default_config.py").write_text(
        "from pydantic import BaseModel, Field\n"
        "from shared.plugin_config_registry import register_plugin_config\n"
        "class Config(BaseModel):\n"
        "    exec_probe_marker: str = Field(default='default-marker', json_schema_extra={'per_agent': True})\n"
        "register_plugin_config(Config)\n"
    )
    (unit_home / "plugins.json").write_text(
        json.dumps({"plugins": {"exec_config_probe": {"enabled": True}}})
    )


@pytest.mark.usefixtures("fake_cancel_event")
async def test_concurrent_turn_configs_reach_real_children_without_cross_talk(
    unit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        "from shared.config import settings\n"
        "from ava._settings import plugins\n"
        "print('CONFIG=' + json.dumps([settings.lm.llm_model, "
        "settings.lm.llm_stream_ttft_timeout_seconds, "
        "plugins.exec_config_probe.exec_probe_marker, "
        "os.environ.get('AVA_AGENT_CONFIG_OVERLAY', 'GONE')]))\n"
    )

    async def execute(agent_id: int) -> list[object]:
        result, *_ = await _run_agent_code(
            AgentState(),
            AvaContext(),
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
        with (
            bind_turn_identity(agent_id),
            bind_agent_config(pins),
            bind_agent_plugin_config({"exec_config_probe": {"exec_probe_marker": marker}}),
        ):
            tasks.append(asyncio.create_task(execute(agent_id)))
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
