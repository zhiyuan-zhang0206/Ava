"""Unit tests for the self-evolution record builder (reference/record.py).

Same import pattern as test_self_evolution_daily_scan.py: the reference dir is
a script dir, not a package, so the module is imported via importlib.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, cast

import pytest

REF_DIR = (
    Path(__file__).resolve().parents[2]
    / "ava_builtins"
    / "skills"
    / "ava-self-evolution"
    / "reference"
)


@pytest.fixture()
def record() -> Any:
    sys.path.insert(0, str(REF_DIR))
    try:
        return cast(Any, importlib.import_module("record"))
    finally:
        sys.path.remove(str(REF_DIR))


@pytest.mark.parametrize(
    ("event", "is_ok", "is_fail"),
    [
        ("exec", True, False),
        ("exec_failed", False, True),
        ("exec_timeout", False, True),
        ("exec_node_timeout", False, True),
        ("exec_cancelled", False, True),
        ("exec_subprocess_aborted", False, True),
        ("exec_subprocess_killed", False, True),
        ("exec_result_envelope_invalid", False, True),
        # informational events are outcomes neither ok nor fail
        ("exec_envelope", False, False),
        ("exec_start", False, False),
        ("exec_output", False, False),
        ("exec_output_chunk", False, False),
        ("turn_end", False, False),
    ],
)
def test_exec_outcome_classification(record: Any, event: str, is_ok: bool, is_fail: bool) -> None:
    assert record._is_exec_ok(event) is is_ok
    assert record._is_exec_fail(event) is is_fail


def test_count_tool_calls_ignores_syntax_warnings(
    record: Any, recwarn: pytest.WarningsRecorder
) -> None:
    """Agent-written code with an invalid escape sequence (a non-raw-string
    regex) parses fine and must not leak a SyntaxWarning into the scan's
    stderr — where it rode along in the schedule ALERT message as
    `<unknown>:N` lines (2026-08-27 00:00 run)."""
    codes = [
        "import ava\nava.files.write('x', 'y')",
        "re.compile('a\\|b')",
        "ava.shell.run('ls')",
    ]
    counts = record.count_tool_calls(codes)
    assert counts == {"ava.files.write": 1, "ava.shell.run": 1}
    assert not [w for w in recwarn.list if issubclass(w.category, SyntaxWarning)]


def _label_record(*, turns: int, exec_failed: int) -> dict[str, Any]:
    return {
        "breached": False,
        "tools_called": {},
        "terminated": False,
        "final_output": "delivered",
        "turns": turns,
        "last_exec_failed": False,
        "followup_prompts": [],
        "corrections": [],
        "peer_feedback": [],
        "exec_failed": exec_failed,
        "compactions": 0,
    }


def _empty_transcript(_agent_id: int) -> list[dict[str, str]]:
    return []


@pytest.mark.parametrize(
    ("turns", "exec_failed", "expected"),
    [
        pytest.param(5, 3, "fumbled", id="short-worker-keeps-three-failure-floor"),
        pytest.param(204, 13, "ok", id="agent-4731-iteration-failures-are-not-a-fumble"),
    ],
)
def test_exec_failure_fumble_threshold_scales_with_turns(
    record: Any, turns: int, exec_failed: int, expected: str
) -> None:
    assert record.label(_label_record(turns=turns, exec_failed=exec_failed)) == expected


def test_broadcast_instruction_is_excluded_from_per_agent_corrections(
    record: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(record, "_transcript", _empty_transcript)

    rec = record.build_record(
        agent_id=7,
        week="2026-08-30",
        events=[],
        log_events=[],
        inbounds=[
            {"source": "user", "content": "task prompt", "is_broadcast": False},
            {
                "source": "user",
                "content": "wrong: pause all work until resumed",
                "is_broadcast": True,
            },
        ],
        meta=("user", "completed", "delivered"),
    )

    assert rec["corrections"] == []
    assert rec["followup_prompts"] == []
    assert rec["label"] == "ok"


def test_targeted_correction_remains_a_per_agent_correction(
    record: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(record, "_transcript", _empty_transcript)

    rec = record.build_record(
        agent_id=7,
        week="2026-08-30",
        events=[],
        log_events=[],
        inbounds=[
            {"source": "user", "content": "task prompt", "is_broadcast": False},
            {"source": "user", "content": "wrong: use the requested path", "is_broadcast": False},
        ],
        meta=("user", "completed", "delivered"),
    )

    assert rec["corrections"] == ["wrong: use the requested path"]
    assert rec["label"] == "fumbled"
