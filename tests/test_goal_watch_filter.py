"""Goal watchers treat lifecycle SSE as a hint and read authoritative status.

No Redis or real gateway is needed: tests serialize the actual event model and
observe whether the watcher requests the target's current status.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import ava
from shared.live_events import EVENT_ADAPTER

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SNIPPET_PATHS = (
    "ava_builtins/skills/ava-goal/reference/watch_idle.py",
    "ava_builtins/skills/ava-watcher/reference/watch_idle.py",
    "ava_builtins/plugins/ava_fleet/skills/ava-fleet/reference/watch_idle.py",
)
_FIXTURE_PATH = _REPO_ROOT / "tests" / "fixtures" / "events" / "agent_updated.json"


def _load_is_target_idle(path: str) -> Callable[[dict[str, Any], int], bool]:
    """Import `_is_target_idle` straight from the skill's reference snippet.

    Importing the snippet runs only its top-level definitions (the
    `if __name__ == "__main__"` block does not fire on import), so this does not
    start the blocking watcher loop.
    """
    spec = importlib.util.spec_from_file_location("goal_watch_idle", _REPO_ROOT / path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn: Callable[[dict[str, Any], int], bool] = vars(module)["_is_target_idle"]
    return fn


@pytest.fixture(params=_SNIPPET_PATHS)
def is_target_idle(request: pytest.FixtureRequest) -> Callable[[dict[str, Any], int], bool]:
    return _load_is_target_idle(request.param)


def _agent_updated_event(agent_id: int) -> dict[str, Any]:
    raw = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    raw["agent_id"] = agent_id
    event = EVENT_ADAPTER.validate_python(raw)
    return json.loads(event.model_dump_json())


@pytest.mark.parametrize("status", ["idling", "running", "restarting", "terminated"])
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
