"""`scripts/content_lint/lint_audit_record.py` — every audit emit site records to Postgres first.

The rule is per function: a call that builds an audit event is a violation unless
the same function also calls (or hands to a runner) a recorder.
"""

from __future__ import annotations

import importlib

_lint = importlib.import_module("scripts.content_lint.lint_audit_record")


def _scopes(src: str) -> list[str]:
    return [scope for scope, _line, _reason in _lint.violations_in_source(src)]


def test_building_an_audit_event_without_recording_is_a_violation() -> None:
    src = (
        "def f(db):\n"
        "    event = prepare_event_log(event_type='spawn', agent_id=1, source='user')\n"
        "    telemetry.prepare_event('audit', 'send_message')\n"
        "    telemetry.emit('audit', 'exit')\n"
    )
    assert _scopes(src) == ["f", "f", "f"]


def test_attribute_calls_are_seen() -> None:
    src = "def f():\n    audit_events.prepare_event_log(event_type='x', agent_id=1)\n"
    assert _scopes(src) == ["f"]


def test_building_and_recording_in_one_function_is_clean() -> None:
    src = (
        "def f(db):\n"
        "    event = prepare_event_log(event_type='spawn', agent_id=1, source='user')\n"
        "    record_audit(db, event)\n"
        "async def g(pool):\n"
        "    event = telemetry.prepare_event('audit', 'exit')\n"
        "    await record_audit_standalone_async(pool, event)\n"
        "def h():\n"
        "    record_audit_standalone_many([prepare_event_log(event_type='x')])\n"
    )
    assert _scopes(src) == []


def test_a_recorder_handed_to_a_runner_counts() -> None:
    src = (
        "async def f():\n"
        "    event = prepare_event_log(event_type='mcp_tool_call')\n"
        "    await asyncio.to_thread(record_audit_reported, event)\n"
    )
    assert _scopes(src) == []


def test_recording_in_another_function_does_not_cover_the_builder() -> None:
    src = (
        "def build():\n"
        "    return prepare_event_log(event_type='spawn', agent_id=1, source='user')\n"
        "def commit(db, event):\n"
        "    record_audit(db, event)\n"
    )
    assert _scopes(src) == ["build"]


def test_non_audit_and_dynamic_categories_are_ignored() -> None:
    src = (
        "def f(category):\n"
        "    telemetry.emit('telemetry', 'turn_end')\n"
        "    telemetry.emit(category, 'turn_end')\n"
        "    emit()\n"
    )
    assert _scopes(src) == []


def test_nested_scope_is_reported_with_its_qualified_name() -> None:
    src = "class C:\n    def m(self):\n        prepare_event_log(event_type='spawn')\n"
    assert _scopes(src) == ["C.m"]


def test_the_repository_has_no_unrecorded_audit_event() -> None:
    assert _lint.main([]) == 0
