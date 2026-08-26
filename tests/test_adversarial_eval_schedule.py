"""The adversarial-eval batch's worker-done predicate.

Pins the 2026-08-26 wedge: freshly spawned workers start as IDLING before
their process boots, and the old predicate (`IDLING or TERMINATED = done`)
read the whole batch as finished seconds after spawning — auditing empty
transcripts while the workers kept running as orphans.
"""

from __future__ import annotations

import importlib

import pytest

from ava.agents import AgentStatus as S

# The schedule module's file name carries a hyphen, so plain `from schedules...`
# import cannot reach it — importlib does.
mod = importlib.import_module("schedules.adversarial-eval-weekly-schedule")


def _status(value: str | None) -> S | None:
    return next((member for member in S if member.value == value), None) if value else None


@pytest.mark.parametrize(
    ("status", "last_message", "expected"),
    [
        ("terminated", None, True),  # the normal self-terminated end
        ("terminated", "done report", True),
        ("idling", "done report", True),  # finished a turn but forgot to self-terminate
        ("idling", None, False),  # fresh spawn: row created idling, process not booted
        ("running", None, False),
        ("running", "mid-task note", False),
        (None, None, False),  # row not yet visible to list_agents
    ],
)
def test_worker_done(
    monkeypatch: pytest.MonkeyPatch, status: str | None, last_message: str | None, expected: bool
) -> None:
    monkeypatch.setattr(mod.ava.agents, "get_last_message", lambda _agent_id: last_message)
    assert mod._worker_done(1234, _status(status)) is expected
