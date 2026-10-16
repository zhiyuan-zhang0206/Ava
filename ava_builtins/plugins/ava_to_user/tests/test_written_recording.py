from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agent.state import build_agent_state
from ava.sdk_surface import process_context
from ava.tests.fixtures.pin_agent import pin_agent
from ava_builtins.plugins.ava_to_user import agent_runtime, plugin
from ava_builtins.plugins.ava_to_user.written import panel_path, record_panel_write
from base.agents.to_user.models import DOCUMENT_FILENAME
from base.packages.plugins.extensions import ExtensionRegistry
from base.paths import workspace_dir

_AGENT = 17
_PANEL_KEY = "ava_to_user__panel_written"


@pytest.fixture
def agent(unit_home: Path) -> int:
    pin_agent(_AGENT)
    return _AGENT


@pytest.fixture
def state() -> Iterator[Any]:
    registry = ExtensionRegistry((("ava_to_user", agent_runtime.contribute()),))
    working = build_agent_state(registry)(messages=[])
    process_context.set_state(working)
    process_context.set_state_update({})
    try:
        yield working
    finally:
        process_context.unbind_exec_turn()


def _panel(agent_id: int) -> Path:
    return workspace_dir(agent_id) / DOCUMENT_FILENAME


def test_only_the_panel_itself_matches(agent: int, unit_home: Path) -> None:
    assert panel_path(_panel(agent), agent_id=agent) == _panel(agent).resolve()
    assert panel_path(str(_panel(agent)), agent_id=agent) == _panel(agent).resolve()
    assert panel_path(workspace_dir(agent) / "report.md", agent_id=agent) is None
    assert panel_path(workspace_dir(agent) / "to_user.html", agent_id=agent) is None
    assert panel_path(_panel(agent + 1), agent_id=agent) is None
    assert panel_path(unit_home / DOCUMENT_FILENAME, agent_id=agent) is None


def test_a_relative_panel_path_resolves_like_the_core(agent: int) -> None:
    assert panel_path(DOCUMENT_FILENAME, agent_id=agent) == _panel(agent).resolve()


def test_a_matched_write_marks_the_bound_state(state: Any, agent: int) -> None:
    record_panel_write(_panel(agent))
    assert getattr(state, _PANEL_KEY) is True
    assert process_context.get_state_update() == {_PANEL_KEY: True}


def test_an_unmatched_write_records_nothing(state: Any, agent: int) -> None:
    record_panel_write(workspace_dir(agent) / "report.md")
    assert getattr(state, _PANEL_KEY) is False
    assert process_context.get_state_update() == {}


def test_recording_outside_an_exec_turn_is_a_noop(agent: int) -> None:
    record_panel_write(_panel(agent))


def test_the_write_wrap_records_a_panel_write(state: Any, agent: int) -> None:
    calls: list[tuple[object, object]] = []

    def inner(path: str | Path, content: str) -> None:
        calls.append((path, content))

    plugin.wrapped_write(inner, _panel(agent), "hello")
    assert calls == [(_panel(agent), "hello")]
    assert getattr(state, _PANEL_KEY) is True


def test_the_write_wrap_lets_its_exception_through_untouched(state: Any, agent: int) -> None:
    def inner(path: str | Path, content: str) -> None:
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError, match="disk full"):
        plugin.wrapped_write(inner, _panel(agent), "hello")
    assert getattr(state, _PANEL_KEY) is False


def test_an_edit_wrap_records_a_panel_edit(state: Any, agent: int) -> None:
    panel = _panel(agent)
    panel.write_text("alpha beta\n", encoding="utf-8")

    def inner(path: str | Path, old: str, new: str, *, replace_all: bool = False) -> None:
        text = Path(path).read_text(encoding="utf-8")
        assert replace_all is False
        Path(path).write_text(text.replace(old, new), encoding="utf-8")

    plugin.wrapped_edit(inner, panel, "beta", "gamma")
    assert panel.read_text(encoding="utf-8") == "alpha gamma\n"
    assert getattr(state, _PANEL_KEY) is True


def test_an_append_wrap_records_a_panel_append(state: Any, agent: int) -> None:
    calls: list[tuple[object, object]] = []

    def inner(path: str | Path, content: str) -> None:
        calls.append((path, content))

    plugin.wrapped_append(inner, _panel(agent), "more")
    assert calls == [(_panel(agent), "more")]
    assert getattr(state, _PANEL_KEY) is True


def test_the_append_wrap_lets_its_exception_through_untouched(state: Any, agent: int) -> None:
    def inner(path: str | Path, content: str) -> None:
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError, match="disk full"):
        plugin.wrapped_append(inner, _panel(agent), "more")
    assert getattr(state, _PANEL_KEY) is False
