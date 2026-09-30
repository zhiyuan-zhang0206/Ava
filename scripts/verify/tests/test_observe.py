"""The observer judges the executed output body, not a model claim or a stray digit."""

from __future__ import annotations

from typing import Any

import pytest

from scripts.verify import observe

REPLY = "the reply"


def _timeline(
    *,
    code: str = "print(1 + 2)",
    output: str = "exit 0\n\n3\n",
    exec_ms: int | None = 12,
    reply: str = REPLY,
) -> list[dict[str, Any]]:
    return [
        {"kind": "agent_code", "payload": code, "exec_ms": None},
        {"kind": "code_output", "payload": output, "exec_ms": exec_ms},
        {"kind": "agent_chat", "payload": reply, "exec_ms": None},
    ]


def test_a_completed_execution_is_recognized() -> None:
    assert observe.execution_completed(_timeline(), REPLY)


@pytest.mark.parametrize(
    "items",
    [
        _timeline(output="exit 0\n\n13\n"),
        _timeline(output="exit 0 at 2026-10-01T03:00:00\n\nnothing printed\n"),
        _timeline(output="3"),
        _timeline(output="cancelled\n\n3\n"),
        _timeline(output="timeout after 30s\n\n3\n"),
        _timeline(exec_ms=None),
        _timeline(code="print(2 + 1)"),
        _timeline(reply="another reply"),
        _timeline()[:2],
        [*_timeline(), {"kind": "code_output", "payload": "exit 0\n\n3\n", "exec_ms": 1}],
    ],
)
def test_anything_short_of_the_executed_output_three_is_not_completion(
    items: list[dict[str, Any]],
) -> None:
    assert not observe.execution_completed(items, REPLY)
