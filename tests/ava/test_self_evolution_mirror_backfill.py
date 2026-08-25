"""mirror_backfill consumer-side id dedup.

Regression lock for Task #1408 (2026-08-26): the mirror-backfill consumer
(``_mirror_fetch_factory``) originally passed mirror rows straight through to
``collect()``, so byte-identical duplicate rows the emitter writes twice
(~7% of mirror rows on 08-24/25, same surrogate id) inflated backfilled
datasets. It now dedupes on the surrogate event id mirror rows carry since
PR #356 (``shared/telemetry.event_id``), matching
``collect._fetch_events_window``'s None-id handling.
"""

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

REF_DIR = (
    Path(__file__).resolve().parents[2]
    / "ava_builtins"
    / "skills"
    / "ava-self-evolution"
    / "reference"
)


@pytest.fixture(scope="module")
def backfill_mod() -> Any:
    """Load the reference script as a module — it is a script, not a package
    (the skill directory name has a hyphen), so import via spec + sys.path.
    Importing is itself the syntax regression lock: the prod runtime copy
    shipped with an unterminated string literal from 2026-08-21 to 08-26 and
    never compiled (no .pyc), so it silently stopped being the fallback."""
    sys.path.insert(0, str(REF_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            "self_ev_mirror_backfill", REF_DIR / "mirror_backfill.py"
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["self_ev_mirror_backfill"] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.pop(0)


def _row(i: int, ts: str, agent_id: int = 7, category: str = "telemetry") -> dict[str, Any]:
    return {
        "id": i,
        "ts": ts,
        "agent_id": agent_id,
        "category": category,
        "event_name": "turn_end",
        "attributes": {},
    }


def _consumer(backfill_mod: Any, rows: dict[str, list[dict[str, Any]]]) -> Any:
    return backfill_mod._mirror_fetch_factory(rows)


def test_consumer_dedups_by_id(backfill_mod: Any) -> None:
    ts = datetime.now(UTC).isoformat()
    rows = {"telemetry": [_row(1, ts), _row(2, ts), _row(1, ts), _row(3, ts)]}

    out = _consumer(backfill_mod, rows)("telemetry", None, None)

    assert [r["id"] for r in out] == [1, 2, 3]


def test_consumer_keeps_first_occurrence_and_order(backfill_mod: Any) -> None:
    ts = datetime.now(UTC).isoformat()
    rows = {
        "telemetry": [_row(1, ts, agent_id=7), _row(2, ts, agent_id=8), _row(1, ts, agent_id=9)]
    }

    out = _consumer(backfill_mod, rows)("telemetry", None, None)

    assert [r["id"] for r in out] == [1, 2]
    assert out[0]["agent_id"] == 7  # first occurrence wins


def test_consumer_passes_none_id_rows_through(backfill_mod: Any) -> None:
    """Pre-#356 mirror files carry no id — there is no key to dedupe on, so
    the rows pass through, exactly like collect._fetch_events_window treats
    None ids (dedup is a silent no-op for them)."""
    ts = datetime.now(UTC).isoformat()
    rows: dict[str, list[dict[str, Any]]] = {
        "telemetry": [
            {"id": None, "ts": ts, "agent_id": 7, "event_name": "a", "attributes": {}},
            {"id": None, "ts": ts, "agent_id": 7, "event_name": "a", "attributes": {}},
            {"id": 1, "ts": ts, "agent_id": 7, "event_name": "b", "attributes": {}},
        ]
    }

    out = _consumer(backfill_mod, rows)("telemetry", None, None)

    assert [r["id"] for r in out] == [None, None, 1]


def test_consumer_agent_filter_combines_with_dedup(backfill_mod: Any) -> None:
    ts = datetime.now(UTC).isoformat()
    rows = {
        "telemetry": [_row(1, ts, agent_id=7), _row(1, ts, agent_id=7), _row(2, ts, agent_id=8)]
    }

    out = _consumer(backfill_mod, rows)("telemetry", None, None, agent_id=7)

    assert [r["id"] for r in out] == [1]


def test_consumer_categories_dedupe_independently(backfill_mod: Any) -> None:
    ts = datetime.now(UTC).isoformat()
    rows = {"telemetry": [_row(1, ts)], "audit": [_row(1, ts, category="audit")]}
    consumer = _consumer(backfill_mod, rows)

    assert [r["id"] for r in consumer("telemetry", None, None)] == [1]
    assert [r["id"] for r in consumer("audit", None, None)] == [1]


def test_consumer_unknown_category_empty(backfill_mod: Any) -> None:
    rows = {"telemetry": [_row(1, datetime.now(UTC).isoformat())]}

    assert _consumer(backfill_mod, rows)("log", None, None) == []


def test_backfill_wires_dedup_into_collect(
    backfill_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """backfill() must hand the deduped row set to collect(): the consumer it
    installs (C._fetch_events_window) drops duplicate ids, keeps the window,
    and feeds both categories before collect() ever sees a row."""
    now = datetime.now(UTC)
    window_from = now - timedelta(days=1)
    ts_in = (window_from + timedelta(minutes=5)).isoformat()
    ts_out = (now - timedelta(days=2)).isoformat()  # outside the window — dropped

    logs = tmp_path / "logs"
    logs.mkdir()
    day = (window_from - timedelta(minutes=1)).date().strftime("%Y%m%d")
    events = [
        _row(1, ts_in),
        _row(1, ts_in),  # true duplicate
        _row(2, ts_in),
        _row(3, ts_out),
        {**_row(4, ts_in), "category": "audit"},
    ]
    (logs / f"events-{day}.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
    )

    monkeypatch.setattr(backfill_mod, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(backfill_mod, "MIRROR_DIR", logs)
    captured: dict[str, list[dict[str, Any]]] = {}

    def fake_collect(days: int, week: str, **kw: Any) -> list[dict[str, Any]]:
        frm, to = kw.get("from_"), kw.get("to")
        captured["telemetry"] = backfill_mod.collect._fetch_events_window("telemetry", frm, to)
        captured["audit"] = backfill_mod.collect._fetch_events_window("audit", frm, to)
        return []

    monkeypatch.setattr(backfill_mod.collect, "collect", fake_collect)

    path = backfill_mod.backfill(1, "test-week")

    assert [r["id"] for r in captured["telemetry"]] == [1, 2]
    assert [r["id"] for r in captured["audit"]] == [4]
    assert path == tmp_path / "self_evolution" / "daily" / "test-week.jsonl"
    assert path.exists()
