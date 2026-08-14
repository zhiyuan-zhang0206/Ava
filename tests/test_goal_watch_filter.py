"""Guard the ava-goal skill's idle-detection filter against the real event wire format.

The ava-goal skill (`ava_builtins/skills/ava-goal/`) launches `reference/watch_idle.py` as a
background watcher. Its `_is_target_idle(event, target_id)` is the one piece of
logic that decides whether a decoded lifecycle update means "the watched agent
just went idle". This test pins that filter to the actual `AgentUpdated` wire
output produced by `shared.live_events`, so a future change to the event schema that
would silently stop the watcher from firing turns this test red instead.

No Redis here: the live pub/sub round-trip is verified out of band against the
dev event channel. This test only needs the wire format, which it gets from the
real pydantic model + the committed `agent_updated.json` fixture.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from shared.live_events import EVENT_ADAPTER

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SNIPPET_PATH = _REPO_ROOT / "ava_builtins" / "skills" / "ava-goal" / "reference" / "watch_idle.py"
_FIXTURE_PATH = _REPO_ROOT / "tests" / "fixtures" / "events" / "agent_updated.json"


def _load_is_target_idle() -> Callable[[dict[str, Any], int], bool]:
    """Import `_is_target_idle` straight from the skill's reference snippet.

    Importing the snippet runs only its top-level definitions (the
    `if __name__ == "__main__"` block does not fire on import), so this does not
    start the blocking watcher loop.
    """
    spec = importlib.util.spec_from_file_location("goal_watch_idle", _SNIPPET_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn: Callable[[dict[str, Any], int], bool] = vars(module)["_is_target_idle"]
    return fn


_is_target_idle = _load_is_target_idle()


def _agent_updated_event(agent_id: int, status: str) -> dict[str, Any]:
    """Build an AgentUpdated wire payload by flipping the committed fixture's
    status through the real pydantic model, guaranteeing wire fidelity."""
    raw = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    raw["agent_id"] = agent_id
    raw["snapshot"]["agent_id"] = agent_id
    raw["snapshot"]["status"] = status
    event = EVENT_ADAPTER.validate_python(raw)
    decoded: dict[str, Any] = json.loads(event.model_dump_json())
    return decoded


def test_fires_when_target_goes_idle() -> None:
    assert _is_target_idle(_agent_updated_event(42, "idling"), 42) is True


def test_ignores_non_idle_status() -> None:
    # The fixture ships with status="running"; only "idling" should fire.
    assert _is_target_idle(_agent_updated_event(42, "running"), 42) is False
    assert _is_target_idle(_agent_updated_event(42, "restarting"), 42) is False


def test_ignores_other_agents() -> None:
    assert _is_target_idle(_agent_updated_event(42, "idling"), 99) is False


def test_ignores_other_event_roles() -> None:
    # A different lifecycle event for the target carries no snapshot — must not
    # match, and must not raise on the missing key.
    assert _is_target_idle({"role": "llm_done", "agent_id": 42}, 42) is False
