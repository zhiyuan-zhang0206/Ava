"""AgentStatus dependency guards for the built-in schedule templates."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SCHEDULES_DIR = Path(__file__).resolve().parents[2] / "schedules"

EXPECTED_STATUS_MEMBERS: dict[str, set[str]] = {
    "c9-daily-report-schedule.py": {
        "IDLING",
        "RUNNING",
        "TERMINATED",
    },
    "adversarial-eval-weekly-schedule.py": {
        "IDLING",
        "TERMINATED",
    },
    "memory-steward-schedule.py": {
        "IDLING",
        "RESTARTING",
        "RUNNING",
        "TERMINATED",
    },
    "model-update-tracker-schedule.py": {
        "IDLING",
        "RUNNING",
        "TERMINATED",
    },
    "self-evolution-daily-schedule.py": {
        "IDLING",
        "RESTARTING",
        "RUNNING",
        "TERMINATED",
    },
    "self-evolution-weekly-schedule.py": {
        "IDLING",
        "RESTARTING",
        "RUNNING",
        "TERMINATED",
    },
    "trace-ship-tempo-schedule.py": set(),
}

SCHEDULE_NAMES = {
    "c9-daily-report-schedule.py": "c9-daily-report",
    "adversarial-eval-weekly-schedule.py": "adversarial-eval-weekly",
    "memory-steward-schedule.py": "memory-arbiter",
    "model-update-tracker-schedule.py": "model-update-tracker",
    "self-evolution-daily-schedule.py": "self-evolution-daily",
    "self-evolution-weekly-schedule.py": "self-evolution-weekly",
    "trace-ship-tempo-schedule.py": "trace-ship-tempo",
}


def _status_references(tree: ast.AST) -> set[str]:
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "S"
    }


def _guard_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ensure_agent_status_members"
    ]


@pytest.mark.parametrize(("filename", "expected"), EXPECTED_STATUS_MEMBERS.items())
def test_builtin_schedule_guards_every_referenced_status_member(
    filename: str, expected: set[str]
) -> None:
    tree = ast.parse((SCHEDULES_DIR / filename).read_text())
    assert _status_references(tree) == expected

    calls = _guard_calls(tree)
    if not expected:
        assert calls == []
        return

    assert len(calls) == 1
    call = calls[0]
    assert isinstance(call.args[0], ast.Name) and call.args[0].id == "S"
    assert ast.literal_eval(call.args[1]) == expected
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    assert ast.literal_eval(keywords["schedule_name"]) == SCHEDULE_NAMES[filename]


def test_missing_member_exits_with_schedule_and_member_names() -> None:
    from schedules.agent_status_guard import ensure_agent_status_members

    class DriftedAgentStatus:
        RUNNING = "running"

    with pytest.raises(SystemExit) as raised:
        ensure_agent_status_members(
            DriftedAgentStatus,
            {"IDLING", "RUNNING", "TERMINATED"},
            schedule_name="drift-probe",
        )

    assert raised.value.code == (
        "schedule 'drift-probe' is missing AgentStatus members: IDLING, TERMINATED"
    )
