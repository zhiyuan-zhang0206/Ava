"""`scripts/content_lint/lint_audit_record.py` — every audit emit site records to Postgres first.

The rule is per function: an enqueue-only `insert_event_log*` call is always a
violation, and a call that builds an audit event is one unless the same function
also calls `record_audit` / `record_audit_standalone`. The frozen baseline is an
exact map, so both a new violation and a stale entry fail.
"""

from __future__ import annotations

import importlib

_lint = importlib.import_module("scripts.content_lint.lint_audit_record")


def _scopes(src: str) -> list[str]:
    return [scope for scope, _line, _reason in _lint.violations_in_source(src)]


def test_enqueue_only_api_is_a_violation_even_when_the_function_records() -> None:
    src = (
        "def f(db):\n"
        "    insert_event_log(event_type='spawn', agent_id=1, source='user')\n"
        "    record_audit(db, event)\n"
    )
    assert _scopes(src) == ["f"]


def test_attribute_calls_are_seen() -> None:
    src = "def f():\n    audit_events.insert_event_log_many(event_type='x', agent_id=1)\n"
    assert _scopes(src) == ["f"]


def test_building_an_audit_event_without_recording_is_a_violation() -> None:
    src = (
        "def f(db):\n"
        "    event = prepare_event_log(event_type='spawn', agent_id=1, source='user')\n"
        "    telemetry.prepare_event('audit', 'send_message')\n"
        "    telemetry.emit('audit', 'exit')\n"
    )
    assert _scopes(src) == ["f", "f", "f"]


def test_building_and_recording_in_one_function_is_clean() -> None:
    src = (
        "def f(db):\n"
        "    event = prepare_event_log(event_type='spawn', agent_id=1, source='user')\n"
        "    record_audit(db, event)\n"
        "async def g():\n"
        "    event = telemetry.prepare_event('audit', 'exit')\n"
        "    record_audit_standalone(event)\n"
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
    src = "class C:\n    def m(self):\n        insert_event_log(event_type='spawn')\n"
    assert _scopes(src) == ["C.m"]


def test_compare_flags_growth_and_stale_entries_but_only_inside_the_judged_files() -> None:
    found = {"a.py::f": 2, "b.py::g": 1}
    baseline = {"a.py::f": 1, "b.py::g": 1, "c.py::h": 1}

    problems = _lint.compare(found, baseline, None)
    assert [p.split(":")[0] for p in problems] == ["a.py", "c.py"]

    assert _lint.compare(found, baseline, frozenset({"b.py"})) == []


def test_the_repository_matches_its_frozen_baseline() -> None:
    assert _lint.main([]) == 0
