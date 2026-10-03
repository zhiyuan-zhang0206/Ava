"""`ava plugins verify` — the loader's contained failures, turned into an exit code.

The 2026-10-03 shape: the declarative rework deleted `register_after_exec` and its siblings; an
out-of-repo plugin under `~/.ava/plugins` that still imported them failed to load at every boot and
the update looked healthy, because the loader skips a broken plugin instead of failing the process.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from base import paths
from base.packages.plugins import load_report
from base.packages.plugins.enable_config import write_local
from cli.commands.extensions.plugins_inspect import cmd_plugins_verify

_PLUGIN_MODULE_PREFIXES = ("ava_builtins.plugins.", "plugins.")


@pytest.fixture(autouse=True)
def _isolate_plugins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    repo = tmp_path / "repo_plugins"
    user = tmp_path / "user_plugins"
    repo.mkdir()
    user.mkdir()
    monkeypatch.setattr(paths, "repo_plugins_dir", lambda: repo)
    monkeypatch.setattr(paths, "plugins_dir", lambda: user)
    monkeypatch.setattr(paths, "plugins_config_path", lambda: tmp_path / "plugins.json")
    before = {k: v for k, v in sys.modules.items() if k.startswith(_PLUGIN_MODULE_PREFIXES)}
    yield
    for key in [
        k for k in sys.modules if k.startswith(_PLUGIN_MODULE_PREFIXES) and k not in before
    ]:
        del sys.modules[key]
    sys.modules.update(before)


def _plugin(name: str, plugin_py: str, **siblings: str) -> None:
    directory = paths.plugins_dir() / name
    directory.mkdir()
    (directory / "plugin.py").write_text(plugin_py)
    for filename, content in siblings.items():
        (directory / filename).write_text(content)


def _enable(*names: str) -> None:
    write_local({"plugins": {name: {"enabled": True} for name in names}})


def test_loadable_plugins_are_green(capsys: pytest.CaptureFixture[str]) -> None:
    _plugin("good", "LOADED = True\n")
    _enable("good")

    assert cmd_plugins_verify() == 0

    assert "RESULT enabled=1 failed=0 rc=0" in capsys.readouterr().out


def test_a_plugin_using_a_deleted_framework_api_is_red_and_the_rest_still_count(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _plugin("good", "LOADED = True\n")
    _plugin(
        "old_hooks",
        "from agent.hooks import register_after_exec\nregister_after_exec(lambda: None)\n",
    )
    _enable("good", "old_hooks")

    assert cmd_plugins_verify() == 1

    out = capsys.readouterr().out
    assert "RESULT enabled=2 failed=1 rc=1" in out
    red = next(line for line in out.splitlines() if line.startswith("RED "))
    assert red.startswith(
        "RED plugin=old_hooks ImportError: cannot import name 'register_after_exec'"
    )
    assert "old_hooks/plugin.py:1" in red


def test_a_broken_runtime_face_is_red_too(capsys: pytest.CaptureFixture[str]) -> None:
    """`agent_runtime.py` is where hooks are declared; it loads on the same path."""
    _plugin("half", "LOADED = True\n", **{"agent_runtime.py": "from agent.hooks import gone\n"})
    _enable("half")

    assert cmd_plugins_verify() == 1

    assert "RED plugin=half ImportError" in capsys.readouterr().out


def test_an_enabled_plugin_missing_from_disk_is_red(capsys: pytest.CaptureFixture[str]) -> None:
    _enable("vanished")

    assert cmd_plugins_verify() == 1

    assert "RED plugin=vanished" in capsys.readouterr().out


def test_a_disabled_broken_plugin_is_never_imported(capsys: pytest.CaptureFixture[str]) -> None:
    _plugin("old_hooks", "raise RuntimeError('boom')\n")
    write_local({"plugins": {"old_hooks": {"enabled": False}}})

    assert cmd_plugins_verify() == 0

    assert "RESULT enabled=0 failed=0 rc=0" in capsys.readouterr().out


def test_nothing_is_emitted_while_verifying_and_the_canonical_reporter_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read-only: no `plugin_load_failed` telemetry from a verify; a later canonical report still emits."""
    emitted: list[tuple[object, ...]] = []

    def record(*args: object, **_kwargs: object) -> None:
        emitted.append(args)

    monkeypatch.setattr("base.telemetry.emit", record)
    _plugin("old_hooks", "raise RuntimeError('boom')\n")
    _enable("old_hooks")

    assert cmd_plugins_verify() == 1
    assert emitted == []

    load_report.report_plugin_load_failure("elsewhere", RuntimeError("later"))
    assert [args[1] for args in emitted] == ["plugin_load_failed"]


def test_a_loader_crash_is_a_tool_error_not_a_red(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(**_: object) -> None:
        raise RuntimeError("loader exploded")

    monkeypatch.setattr("agent.extensions.load_extensions", boom)

    assert cmd_plugins_verify() == 2

    out = capsys.readouterr().out
    assert "TOOL-ERROR" in out and "RESULT enabled=0 failed=0 rc=2" in out
