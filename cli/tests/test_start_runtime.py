"""A start runs the checkout that loaded this code, and the lifecycle rechecks that identity."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

from base.deploy.release import runtime_interpreter
from cli.start_runtime import StartRuntime, StartRuntimeChangedError


def test_loaded_runtime_package_is_the_checkout_it_was_imported_from() -> None:
    """The import root is anchored on runtime_interpreter's own path inside the tree."""
    assert runtime_interpreter.loaded_runtime()[2] == Path(__file__).resolve().parents[2]


def test_development_runtime_is_the_checkout_and_the_running_interpreter(tmp_path: Path) -> None:
    runtime = StartRuntime.development(tmp_path)

    assert runtime.code_root == runtime.cwd == tmp_path
    assert runtime.interpreter == Path(sys.executable).absolute()
    assert runtime.module_argv("services.supervision.ava_root", "--run-dir", "/run") == [
        str(runtime.interpreter),
        "-m",
        "services.supervision.ava_root",
        "--run-dir",
        "/run",
    ]


def test_validate_refuses_a_runtime_that_changed_after_capture(tmp_path: Path) -> None:
    runtime = StartRuntime.development(tmp_path)
    runtime.validate()

    with pytest.raises(StartRuntimeChangedError, match="changed"):
        replace(runtime, cwd=tmp_path / "elsewhere").validate()
    with pytest.raises(StartRuntimeChangedError, match="changed"):
        replace(runtime, interpreter=Path("/usr/bin/other-python")).validate()


def test_a_leftover_release_selector_no_longer_decides_a_source_start(tmp_path: Path) -> None:
    """A source start admits its own checkout; nothing under `releases/` is consulted."""
    (tmp_path / "releases").mkdir()
    (tmp_path / "releases/current-release").symlink_to(tmp_path / "absent")

    StartRuntime.development(tmp_path).validate()
