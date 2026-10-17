from __future__ import annotations

from typing import Any, cast

import pytest

from base.db import Database
from meiva_builtins.skills.tests.skill_script import load_skill_script

record_module = load_skill_script("platform", "meiva-self-evolution", "scripts", "record.py")


@pytest.fixture()
def record() -> Any:
    return cast(Any, record_module)


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
    codes = [
        "import meiva\nmeiva.files.write('x', 'y')",
        "re.compile('a\\|b')",
        "meiva.shell.run('ls')",
    ]
    counts = record.count_tool_calls(codes)
    assert counts == {"meiva.files.write": 1, "meiva.shell.run": 1}
    assert not [w for w in recwarn.list if issubclass(w.category, SyntaxWarning)]


def _label_record(*, turns: int, exec_failed: int) -> dict[str, Any]:
    return {
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


def _empty_transcript(_agent_id: int, *, database: Database) -> list[dict[str, str]]:
    return []


def _build(
    record: Any,
    database: Database,
    *,
    inbounds: list[dict[str, Any]] | None = None,
    transcript: list[dict[str, str]] | None = None,
    spawner: str = "user",
) -> dict[str, Any]:
    return record.build_record(
        database=database,
        agent_id=7,
        week="2026-08-30",
        events=[],
        log_events=[],
        inbounds=inbounds or [],
        transcript=transcript if transcript is not None else [],
        meta=(spawner, "terminated", ""),
    )


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
    record: Any, monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    monkeypatch.setattr(record, "_transcript", _empty_transcript)

    rec = record.build_record(
        database=database,
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
    record: Any, monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    monkeypatch.setattr(record, "_transcript", _empty_transcript)

    rec = record.build_record(
        database=database,
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


@pytest.mark.parametrize(
    ("spawner", "inbounds", "transcript", "expected", "source"),
    [
        pytest.param(
            "agent:1",
            [{"source": "user", "content": "human brief", "is_broadcast": False}],
            [],
            "human brief",
            "human",
            id="human-first",
        ),
        pytest.param(
            "agent:1",
            [{"source": "ui:web", "content": "web console brief", "is_broadcast": False}],
            [],
            "web console brief",
            "human",
            id="legacy-web-console-is-human",
        ),
        pytest.param(
            "agent:1",
            [{"source": "agent:1", "content": "spawn brief", "is_broadcast": False}],
            [],
            "spawn brief",
            "spawner",
            id="spawner-brief",
        ),
        pytest.param(
            "agent:9",
            [{"source": "schedule:7", "content": "scheduled brief", "is_broadcast": False}],
            [],
            "scheduled brief",
            "schedule",
            id="schedule-brief",
        ),
        pytest.param(
            "agent:1",
            [],
            [
                {"type": "human", "content": "[system] Your Agent ID is 7."},
                {"type": "human", "content": "inherited brief"},
            ],
            "inherited brief",
            "transcript",
            id="transcript-fallback",
        ),
        pytest.param(
            "agent:1",
            [],
            [{"type": "human", "content": "[system] Your Agent ID is 7."}],
            "",
            "",
            id="preamble-only",
        ),
    ],
)
def test_task_prompt_origin(
    record: Any,
    database: Database,
    spawner: str,
    inbounds: list[dict[str, Any]],
    transcript: list[dict[str, str]],
    expected: str,
    source: str,
) -> None:
    rec = _build(record, database, inbounds=inbounds, transcript=transcript, spawner=spawner)

    assert rec["task_prompt"] == expected
    assert rec["task_prompt_source"] == source


def test_the_human_message_that_carried_the_task_is_not_a_followup(
    record: Any, database: Database
) -> None:
    inbounds = [
        {"source": "agent:1", "content": "spawn brief", "is_broadcast": False},
        {"source": "user", "content": "also check the logs", "is_broadcast": False},
        {"source": "user", "content": "and then the metrics", "is_broadcast": False},
    ]

    rec = _build(record, database, inbounds=inbounds, spawner="agent:1")

    assert rec["task_prompt"] == "also check the logs"
    assert rec["task_prompt_source"] == "human"
    assert rec["followup_prompts"] == ["and then the metrics"]


def test_task_prompt_of_falls_back_to_the_transcript(record: Any) -> None:
    legacy = {
        "task_prompt": "",
        "transcript": [
            {"type": "human", "content": "[system] Your Agent ID is 7."},
            {"type": "human", "content": "Fix the flaky test."},
        ],
    }
    collected = {"task_prompt": "Fix the flaky test.", "transcript": []}

    assert record.task_prompt_of(legacy) == "Fix the flaky test."
    assert record.task_prompt_of(collected) == "Fix the flaky test."


def test_msg_text_skips_thinking_and_tool_use_blocks(record: Any) -> None:
    silent_turn = [
        {"type": "thinking", "thinking": "internal reasoning"},
        {"type": "tool_use", "name": "execute_code", "input": {"code": "import meiva"}},
    ]
    spoken_turn = [
        {"type": "thinking", "thinking": "internal reasoning"},
        {"type": "text", "text": "Checking the logs now."},
        {"type": "tool_use", "name": "execute_code", "input": {"code": "import meiva"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
    ]

    assert record._msg_text(silent_turn) == ""
    assert record._msg_text(spoken_turn) == "Checking the logs now. image_url"
