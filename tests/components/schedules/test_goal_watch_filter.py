"""Goal watchers treat lifecycle SSE as a hint and read authoritative status.

No Redis or real gateway is needed: tests serialize the actual event model and
observe whether the watcher requests the target's current status. The poll
fallback reads that same status, never the database directly.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import ava
from base.events.live.projection import EVENT_ADAPTER

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SNIPPET_PATHS = (
    "ava_builtins/skills/coordination/ava-goal/scripts/watch_idle.py",
    "ava_builtins/skills/coordination/ava-being-a-long-running-agent/scripts/watch_idle.py",
    "ava_builtins/plugins/ava_fleet/skills/ava-fleet/reference/watch_idle.py",
)
_FIXTURE_PATH = _REPO_ROOT / "tests" / "fixtures" / "events" / "agent" / "agent_updated.json"


def _load_snippet(path: str) -> ModuleType:
    """Import the skill's reference snippet as a module.

    Importing the snippet runs only its top-level definitions (the
    `if __name__ == "__main__"` block does not fire on import), so this does not
    start the blocking watcher loop.
    """
    spec = importlib.util.spec_from_file_location("goal_watch_idle", _REPO_ROOT / path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_is_target_idle(path: str) -> Callable[[dict[str, Any], int], bool]:
    fn: Callable[[dict[str, Any], int], bool] = vars(_load_snippet(path))["_is_target_idle"]
    return fn


@pytest.fixture(params=_SNIPPET_PATHS)
def is_target_idle(request: pytest.FixtureRequest) -> Callable[[dict[str, Any], int], bool]:
    return _load_is_target_idle(request.param)


def _agent_updated_event(agent_id: int) -> dict[str, Any]:
    raw = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    raw["agent_id"] = agent_id
    event = EVENT_ADAPTER.validate_python(raw)
    return json.loads(event.model_dump_json())


@pytest.mark.parametrize("status", ["idling", "running", "terminated"])
def test_target_hint_reads_current_status(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    is_target_idle: Callable[[dict[str, Any], int], bool],
) -> None:
    requested: list[int] = []

    def get_status(agent_id: int) -> str:
        requested.append(agent_id)
        return status

    monkeypatch.setattr(ava.agents, "get_status", get_status)
    assert is_target_idle(_agent_updated_event(42), 42) is (status == "idling")
    assert requested == [42]


@pytest.mark.parametrize(
    "event",
    [
        {"role": "agent_updated", "agent_id": 99},
        {"role": "llm_done", "agent_id": 42},
    ],
)
def test_other_events_do_not_fetch_status(
    monkeypatch: pytest.MonkeyPatch,
    event: dict[str, Any],
    is_target_idle: Callable[[dict[str, Any], int], bool],
) -> None:
    def forbidden(_agent_id: int) -> None:
        pytest.fail("unrelated events must not trigger a status read")

    monkeypatch.setattr(ava.agents, "get_status", forbidden)
    assert is_target_idle(event, 42) is False


@pytest.mark.parametrize("path", _SNIPPET_PATHS)
def test_poll_fallback_reads_status_until_idle(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    """The fallback polls the authoritative status and skips a round on a read error."""
    module = _load_snippet(path)
    replies: list[str | Exception] = [RuntimeError("gateway restarting"), "running", "idling"]
    requested: list[int] = []
    notified: list[int] = []

    def get_status(agent_id: int) -> str:
        requested.append(agent_id)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(ava.agents, "get_status", get_status)
    monkeypatch.setattr(module, "_notify", notified.append)
    vars(module)["_watch_via_poll"](42, interval_s=0)
    assert requested == [42, 42, 42]
    assert notified == [42]
