"""Fresh executable preludes are explicit; imports and embedded calls keep sinks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]


_CHILD = Path(__file__).with_name("_logging_entry_child.py")


def run_probe(scenario: str, home: Path, *extra: str) -> dict[str, object]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("AVA_")}
    env["AVA_HOME"] = str(home)
    child = subprocess.run(  # noqa: S603 — this interpreter, fixed child script, private home
        [sys.executable, "-I", "-B", str(_CHILD), scenario, str(_ROOT), *extra],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    return json.loads(child.stdout.splitlines()[-1])


def test_import_and_embedded_dispatch_preserve_caller_logging(tmp_path: Path) -> None:
    observed = run_probe("import_and_embedded_dispatch", tmp_path / "home")
    assert observed["records"] == ["after import", "after embedded dispatch"]


def test_fresh_entry_prepares_before_dispatch_without_telemetry(tmp_path: Path) -> None:
    observed = run_probe("fresh_entry", tmp_path / "home")
    assert observed == {"records": [], "result": 17, "otel": []}


@pytest.mark.parametrize("role", ["gateway", "cli", "subprocess"])
def test_role_initialization_keeps_caller_sink_and_first_attribution(
    role: str, tmp_path: Path
) -> None:
    observed = run_probe("role_initialization", tmp_path / "home", role)
    assert observed["marker_count"] == 1
    assert observed["agent"] == ("7" if role == "subprocess" else "-")
    assert observed["drained"] == "completed"


@pytest.mark.parametrize("executable", ["console", "cli", "cli.main"])
def test_fresh_cli_executables_stay_quiet(executable: str, tmp_path: Path) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("AVA_")}
    home = tmp_path / "home"
    env["AVA_HOME"] = str(home)
    argv = (
        [str(_ROOT / ".venv/bin/ava")]
        if executable == "console"
        else [sys.executable, "-m", executable]
    )
    child = subprocess.run(  # noqa: S603 — fixed actual entry and help arguments
        [*argv, "--help"],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    assert "usage:" in child.stdout
    assert child.stderr == ""
    assert not (home / "logs").exists()


def test_no_agent_child_prepares_before_main_without_telemetry(tmp_path: Path) -> None:
    observed = run_probe("no_agent_child", tmp_path / "home")
    assert observed == {"records": [], "loaded": []}


def test_fresh_ops_prepares_before_configuration(tmp_path: Path) -> None:
    observed = run_probe("fresh_ops", tmp_path / "home")
    assert observed == {"reached": True, "records": []}
