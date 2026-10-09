"""A fresh execution child reports its original plugin boot error before running code."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent.graph.exec.protocol import read_result, write_request
from tests.fixtures.pin_agent import exec_context


@pytest.mark.parametrize("error_type", ["AttributeError", "RuntimeError", "OSError"])
def test_child_plugin_binding_failure_keeps_original_boot_error(
    tmp_path: Path, error_type: str
) -> None:
    home = tmp_path / "home"
    plugin = home / "plugins" / "boot_failure"
    plugin.mkdir(parents=True)
    (plugin / "plugin.py").write_text(
        "from types import SimpleNamespace\n"
        "from base.packages.plugins.extensions import PluginContributions, SdkNamespace\n"
        "def contribute():\n"
        "    return PluginContributions(sdk_namespaces=(\n"
        "        SdkNamespace('boot_failure', SimpleNamespace()),))\n"
    )
    (plugin / "default_config.py").write_text(
        "from pydantic import BaseModel, Field, model_validator\n"
        "from base.packages.plugins.extensions import PluginContributions\n"
        "class Config(BaseModel):\n"
        "    value: int = Field(default=1, json_schema_extra={'per_agent': True})\n"
        "    @model_validator(mode='after')\n"
        "    def broken(self):\n"
        f"        raise {error_type}('original config binding bug')\n"
        "def contribute():\n"
        "    return PluginContributions(config=Config)\n"
    )
    image = home / "configs" / "boot_failure" / "config.json"
    image.parent.mkdir(parents=True)
    image.write_text('{"value": 1}')
    request, result = tmp_path / "request.json", tmp_path / "result.json"
    marker = tmp_path / "user-code-ran"
    write_request(
        request,
        code=f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
        context=exec_context(None).describe(),
        timeout_s=10,
        state=None,
        incarnation=None,
    )
    env = os.environ | {
        "AVA_HOME": str(home),
        "AVA_PROCESS_PROFILE": "agent",
        "AVA_EXEC_REQUEST_FILE": str(request),
        "AVA_EXEC_RESULT_FILE": str(result),
        # The old skipped-binding path proceeded here and replaced the bug with KeyError.
        "AVA_AGENT_CONFIG_OVERLAY": json.dumps({"value": 2}),
    }
    env.pop("AVA_AGENT_ID", None)
    process = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-m", "agent.execution.child"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    payload = read_result(result)
    assert payload.kind == "crashed" and payload.code_reached is False
    assert payload.exc_type == error_type
    assert payload.exc_msg == "original config binding bug"
    assert payload.full_traceback is not None
    assert "default_config.py" in payload.full_traceback
    assert f"raise {error_type}" in payload.full_traceback
    assert not marker.exists()
    assert image.read_text() == '{"value": 1}'
