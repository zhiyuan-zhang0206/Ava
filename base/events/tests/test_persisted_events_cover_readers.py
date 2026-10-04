"""Every event a Postgres reader queries by name is declared `persist=True`.

`telemetry_events` stores the events somebody reads out of it (`base.telemetry.event_store.is_persisted`).
A reader that names an event the registry does not persist would read an empty set without
failing, so this test collects the names the readers query and requires each to be persisted:

- the SQL literals of every module that reads `telemetry_events` (`event_name = '...'`,
  `event_name IN (...)`, `starts_with(event_name, '...')`), found by scanning the source;
- the name lists the readers pass as parameters, imported from the modules that own them;
- the events of the inspector metrics, rendered from the metric registry.

A new reader adds its name to one of those three places (or is found by the scan) and the test
names the event to mark.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from base.events.contract import EVENTS, LLM_ERROR_FAMILY, family_events

_ROOT = Path(__file__).resolve().parents[3]
_SKIPPED_DIRS = frozenset(
    {".git", ".venv", ".worktrees", "node_modules", "tests", "__pycache__", "migrations"}
)
_EQUALS = re.compile(r"event_name\s*=\s*'([^']+)'")
_IN_LIST = re.compile(r"event_name\s+IN\s*\(([^)]*)\)")
_STARTS_WITH = re.compile(r"starts_with\(\s*(?:\w+\.)?event_name\s*,\s*'([^']+)'\s*\)")
_QUOTED = re.compile(r"'([^']+)'")
_LOGQL_NAMES = re.compile(r"event_name\s*=~?\s*\"([^\"]+)\"")


def _reader_sources() -> list[Path]:
    paths: list[Path] = []
    for directory, subdirectories, files in os.walk(_ROOT):
        subdirectories[:] = [name for name in subdirectories if name not in _SKIPPED_DIRS]
        for name in files:
            if name.endswith(".py") and not name.startswith(("test_", "conftest")):
                path = Path(directory) / name
                if "telemetry_events" in path.read_text(encoding="utf-8"):
                    paths.append(path)
    return paths


def _string_constants(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return "\n".join(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def _queried_by_sql() -> tuple[dict[str, Path], dict[str, Path]]:
    """`(names, prefixes)` the readers' SQL literals select on, each with the file naming it."""
    names: dict[str, Path] = {}
    prefixes: dict[str, Path] = {}
    for path in _reader_sources():
        text = _string_constants(path)
        for match in _EQUALS.finditer(text):
            names[match[1]] = path
        for match in _IN_LIST.finditer(text):
            for quoted in _QUOTED.findall(match[1]):
                names[quoted] = path
        for match in _STARTS_WITH.finditer(text):
            prefixes[match[1]] = path
    return names, prefixes


def _self_evolution_names() -> set[str]:
    """The names `ava-self-evolution` reads from `/api/events` telemetry (record.py)."""
    path = _ROOT / "ava_builtins/skills/platform/ava-self-evolution/scripts/record.py"
    failures: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "_EXEC_FAIL_EVENTS" for t in node.targets)
            and isinstance(node.value, ast.Call)
        ):
            failures = set(ast.literal_eval(node.value.args[0]))
    assert failures, "record.py no longer declares _EXEC_FAIL_EVENTS"
    return {"llm_usage", "turn_end", "code", "exec", "plugin_activation", *failures}


def _parameter_lists() -> dict[str, set[str]]:
    from base.telemetry.metrics import aggregate_sql
    from gateway.lgtm import telemetry_staleness
    from gateway.run_timeline import router as run_timeline
    from services.upkeep.events_maintenance import observed_metrics

    return {
        "aggregate_sql.EXEC_FAILURE_EVENTS": set(aggregate_sql.EXEC_FAILURE_EVENTS),
        "aggregate_sql.LIFECYCLE_EVENTS": set(aggregate_sql.LIFECYCLE_EVENTS),
        "ops_series LLM error family": set(family_events(LLM_ERROR_FAMILY)),
        "run_timeline._TURN_EVENTS": set(run_timeline._TURN_EVENTS),
        "run_timeline._SESSION_START_EVENTS": set(run_timeline._SESSION_START_EVENTS),
        "run_timeline._COMPACT_EVENTS": set(run_timeline._COMPACT_EVENTS),
        "telemetry_staleness.HEARTBEAT_EVENT": {telemetry_staleness.HEARTBEAT_EVENT},
        "observed_metrics recovery scan": set(observed_metrics._EVENT_NAMES),
        "ava-self-evolution record.py": _self_evolution_names(),
    }


def _inspector_names() -> set[str]:
    from gateway.inspect import _plugin_metrics

    names: set[str] = set()
    for spec in _plugin_metrics._load_plugin_metrics():
        if "inspector" not in spec.output:
            continue
        names.add(spec.event_name)
        for query in [spec.query, *(spec.targets or [])]:
            for group in _LOGQL_NAMES.findall(str(query)):
                names.update(group.split("|"))
    names.discard("{event_name}")
    return names


def _unpersisted(names: set[str]) -> list[str]:
    """Registered telemetry or log events among `names` that are not declared `persist=True`."""
    return sorted(
        name
        for name in names
        if name in EVENTS
        and (
            EVENTS[name].category in ("telemetry", "log")
            or "telemetry" in EVENTS[name].extra_categories
        )
        and not EVENTS[name].persist
    )


def test_the_source_scan_still_finds_the_readers() -> None:
    names, _prefixes = _queried_by_sql()
    # A pattern that stops matching must fail here, not silently weaken the guard below.
    assert {"llm_usage", "turn_end", "sse_drop", "service_started", "syntax_fix"} <= set(names)
    assert len(_reader_sources()) >= 10


def test_events_the_sql_readers_name_are_persisted() -> None:
    names, prefixes = _queried_by_sql()
    missing = {name: names[name] for name in _unpersisted(set(names))}
    for prefix, path in prefixes.items():
        for name in _unpersisted({n for n in EVENTS if n.startswith(prefix)}):
            missing[name] = path
    assert not missing, (
        "events a telemetry_events reader queries by name, without persist=True "
        f"(declare it in base/events/declarations/): { {k: str(v.relative_to(_ROOT)) for k, v in missing.items()} }"
    )


def test_name_lists_the_readers_pass_are_persisted() -> None:
    missing = {
        source: _unpersisted(names)
        for source, names in _parameter_lists().items()
        if _unpersisted(names)
    }
    assert not missing, f"events a reader's name list selects, without persist=True: {missing}"


def test_inspector_metrics_read_only_persisted_events() -> None:
    names = _inspector_names()
    assert {"llm_usage", "turn_end", "passive_recall", "recall_filter"} <= names
    assert not _unpersisted(names), (
        f"inspector metrics over events without persist=True: {_unpersisted(names)}"
    )
