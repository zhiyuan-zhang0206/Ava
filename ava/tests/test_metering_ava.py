"""SDK metering at the `ava` root: the MCP call funnel and the SDK events a plain import installs."""

from __future__ import annotations

from pathlib import Path

from ava.sdk_surface import metering


def test_install_wraps_and_restores_mcp_call_funnel() -> None:
    """install()/uninstall() wrap the ava.mcps._call_raw funnel so dynamic MCP tool
    calls are metered, and restore it on teardown."""
    import ava.mcps

    # `ava` is a process-global singleton and `load_extensions` installs the
    # recorders as a side effect, so any earlier test in this xdist worker that
    # loaded plugins leaves the funnel already wrapped — install() then correctly
    # no-ops and the wrap assertion below reads as a failure. Which tests share a
    # worker is not deterministic under `-n`, so take a clean baseline first.
    metering.uninstall()
    before = ava.mcps._call_raw
    metering.install()
    try:
        assert ava.mcps._call_raw is not before
        assert ava.mcps._call_raw in metering._RECORDERS
    finally:
        metering.uninstall()
    assert ava.mcps._call_raw is before


def test_plain_python_import_installs_sdk_events(tmp_path: Path) -> None:
    import json
    import os
    import subprocess
    import sys

    target = tmp_path / "input.txt"
    target.write_text("hello")
    code = """
import json, sys
import ava
from base import telemetry
from base.agents.sdk import call_policy as sdk_call_policy
sdk_call_policy.policy = sdk_call_policy.SamplingPolicy
rows = []
telemetry.emit = lambda *args, **kwargs: rows.append(kwargs)
assert ava.files.read(sys.argv[1]) == "hello"
print(json.dumps(rows))
"""
    result = subprocess.run(  # noqa: S603 — fixed Python code and an isolated fixture path
        [sys.executable, "-c", code, str(target)],
        env={**os.environ, "AVA_AGENT_ID": "42"},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    rows = json.loads(result.stdout)
    assert len(rows) == 1
    assert rows[0]["agent_id"] == 42
    assert rows[0]["attributes"]["fn"] == "files.read"
    assert rows[0]["attributes"]["sample_rate"] == 1
