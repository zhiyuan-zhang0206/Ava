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
import os
import sys
import time
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


def _no_records(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    """collect() stub for tests that only exercise the mirror reading."""
    return []


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


def _write_mirror_day(logs: Path, day: str, events: list[dict[str, Any]]) -> None:
    (logs / f"events-{day}.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
    )


def _window_days(now: datetime, days: int) -> list[str]:
    window_from = now - timedelta(days=days)
    start = (window_from - timedelta(minutes=1)).date()
    end = now.date()
    out: list[str] = []
    d = start
    while d <= end:
        out.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return out


def test_backfill_wires_dedup_into_collect(
    backfill_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """backfill() must hand the deduped row set to collect(): the consumer it
    installs (collect._fetch_events_window) drops duplicate ids, keeps the
    window, and feeds both categories before collect() ever sees a row."""
    now = datetime.now(UTC)
    window_from = now - timedelta(days=1)
    ts_in = (window_from + timedelta(minutes=5)).isoformat()
    ts_out = (now - timedelta(days=2)).isoformat()  # outside the window — dropped

    logs = tmp_path / "logs"
    logs.mkdir()
    days = _window_days(now, 1)
    events = [
        _row(1, ts_in),
        _row(1, ts_in),  # true duplicate
        _row(2, ts_in),
        _row(3, ts_out),
        {**_row(4, ts_in), "category": "audit"},
    ]
    _write_mirror_day(logs, days[0], events)
    for day in days[1:]:  # every window day must exist — missing days are not silent
        _write_mirror_day(logs, day, [])

    monkeypatch.setattr(backfill_mod, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(backfill_mod, "MIRROR_DIR", logs)
    captured: dict[str, list[dict[str, Any]]] = {}

    def fake_collect(days: int, week: str, **kw: Any) -> list[dict[str, Any]]:
        frm, to = kw.get("from_"), kw.get("to")
        # Consume the installed consumer inside collect() — the exact point the
        # real pipeline consumes it, while the temp rows still exist (Task
        # #1995: the disk-backed consumer streams and is valid during collect).
        captured["telemetry"] = list(
            backfill_mod.collect._fetch_events_window("telemetry", frm, to)
        )
        captured["audit"] = list(backfill_mod.collect._fetch_events_window("audit", frm, to))
        return []

    monkeypatch.setattr(backfill_mod.collect, "collect", fake_collect)

    path, missing = backfill_mod.backfill(1, "test-week")

    assert [r["id"] for r in captured["telemetry"]] == [1, 2]
    assert [r["id"] for r in captured["audit"]] == [4]
    assert missing == []
    assert path == tmp_path / "self_evolution" / "daily" / "test-week.jsonl"
    assert path.exists()


def test_backfill_reports_missing_days_and_exits_nonzero(
    backfill_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    """A missing mirror day must never be silent: backfill() returns it, the
    per-day counts print, and the CLI exits non-zero — a partial window must
    not silently become a partial dataset (collect's iron rule)."""
    now = datetime.now(UTC)
    window_from = now - timedelta(days=1)
    logs = tmp_path / "logs"
    logs.mkdir()
    days = _window_days(now, 1)
    # Only the first window day exists; the rest are missing.
    _write_mirror_day(logs, days[0], [_row(1, (window_from + timedelta(minutes=5)).isoformat())])

    monkeypatch.setattr(backfill_mod, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(backfill_mod, "MIRROR_DIR", logs)
    monkeypatch.setattr(backfill_mod.collect, "collect", _no_records)

    path, missing = backfill_mod.backfill(1, "test-week")

    assert missing == days[1:]
    assert path.exists()
    out = capsys.readouterr().out
    assert f"{days[0]}: 1 rows in window" in out  # per-day count printed
    assert "warning: mirror file missing for " + ", ".join(days[1:]) in out

    monkeypatch.setattr(sys, "argv", ["mirror_backfill.py", "1", "test-week"])
    with pytest.raises(SystemExit) as exc:
        backfill_mod.main()
    assert exc.value.code == 1


def test_main_exits_zero_when_all_days_present(
    backfill_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CLI success path: no missing days -> exit 0."""
    now = datetime.now(UTC)
    logs = tmp_path / "logs"
    logs.mkdir()
    for day in _window_days(now, 1):
        _write_mirror_day(logs, day, [])
    monkeypatch.setattr(backfill_mod, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(backfill_mod, "MIRROR_DIR", logs)
    monkeypatch.setattr(backfill_mod.collect, "collect", _no_records)
    monkeypatch.setattr(sys, "argv", ["mirror_backfill.py", "1", "test-week"])

    with pytest.raises(SystemExit) as exc:
        backfill_mod.main()
    assert exc.value.code == 0


# ── Task #1995: disk-backed consumer + two-phase backfill (GC-storm fix) ──


def _write_staged(
    tmp_path: Path, per_category: dict[str, list[dict[str, Any]]]
) -> tuple[Path, dict[str, list[tuple[str, int]]]]:
    """Replicate backfill() phase 1 on a small scale: per-category temp files
    holding the rows' original line bytes, plus a (ts, byte offset) index in
    file order — exactly the layout the disk-backed consumer streams."""
    tmpdir = tmp_path / "staged"
    tmpdir.mkdir()
    index: dict[str, list[tuple[str, int]]] = {}
    for cat, rows in per_category.items():
        p = tmpdir / f"{cat}.jsonl"
        idx: list[tuple[str, int]] = []
        with p.open("w") as f:
            for r in rows:
                idx.append((r["ts"], f.tell()))
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        index[cat] = idx
    return tmpdir, index


def _dir_consumer(backfill_mod: Any, tmpdir: Path, index: dict[str, list[tuple[str, int]]]) -> Any:
    return backfill_mod._mirror_fetch_factory_from_dir(tmpdir, index)


def test_dir_consumer_dedupes_and_filters_like_memory_consumer(
    backfill_mod: Any, tmp_path: Path
) -> None:
    """The disk-backed consumer must be semantics-identical to the in-memory
    one: id dedup (first wins), None-id passthrough, agent filter before
    dedup, unknown category empty (Task #1995 rewired backfill() to it)."""
    ts = datetime.now(UTC).isoformat()
    tmpdir, index = _write_staged(
        tmp_path,
        {
            "telemetry": [
                _row(1, ts, agent_id=7),
                _row(1, ts, agent_id=7),  # true duplicate
                _row(2, ts, agent_id=8),
                {"id": None, "ts": ts, "agent_id": 7, "event_name": "a", "attributes": {}},
                {"id": None, "ts": ts, "agent_id": 7, "event_name": "a", "attributes": {}},
            ]
        },
    )
    consumer = _dir_consumer(backfill_mod, tmpdir, index)

    assert [r["id"] for r in consumer("telemetry", None, None)] == [1, 2, None, None]
    assert [r["id"] for r in consumer("telemetry", None, None, agent_id=7)] == [1, None, None]
    assert [r["id"] for r in consumer("audit", None, None)] == []
    assert [r["id"] for r in consumer("log", None, None)] == []


def test_dir_consumer_streams_in_ts_order(backfill_mod: Any, tmp_path: Path) -> None:
    """The consumer sorts by ts (stable, file order on ties) — the same
    deterministic order the old in-memory path sorted before deduping, so
    last_exec_failed-style order-sensitive signals see identical rows."""
    ts_early = datetime.now(UTC).isoformat()
    ts_late = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()
    tmpdir, index = _write_staged(
        tmp_path,
        {
            "telemetry": [
                _row(3, ts_late),
                _row(1, ts_early),
                _row(2, ts_early),
                _row(3, ts_late),  # duplicate of the first — first in ts order wins
            ]
        },
    )
    consumer = _dir_consumer(backfill_mod, tmpdir, index)

    assert [r["id"] for r in consumer("telemetry", None, None)] == [1, 2, 3]


def test_backfill_merges_days_and_cleans_up(
    backfill_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Task #1995 two-phase backfill: rows from every window day land in the
    same per-category temp file (dedup across days still applies), the temp
    dir is removed after collect, and the cyclic GC is left enabled."""
    now = datetime.now(UTC)
    window_from = now - timedelta(days=2)
    logs = tmp_path / "logs"
    logs.mkdir()
    days = _window_days(now, 2)
    ts_day0 = (window_from + timedelta(minutes=5)).isoformat()
    ts_day1 = (window_from + timedelta(days=1, minutes=5)).isoformat()
    _write_mirror_day(logs, days[0], [_row(1, ts_day0)])
    _write_mirror_day(logs, days[1], [_row(1, ts_day1), _row(2, ts_day1)])  # id 1 dup across days
    _write_mirror_day(logs, days[2], [])  # today's (still-open) file must exist too

    monkeypatch.setattr(backfill_mod, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(backfill_mod, "MIRROR_DIR", logs)
    captured: dict[str, list[dict[str, Any]]] = {}

    def fake_collect(days: int, week: str, **kw: Any) -> list[dict[str, Any]]:
        frm, to = kw.get("from_"), kw.get("to")
        captured["telemetry"] = list(
            backfill_mod.collect._fetch_events_window("telemetry", frm, to)
        )
        return []

    monkeypatch.setattr(backfill_mod.collect, "collect", fake_collect)

    path, missing = backfill_mod.backfill(2, "test-week")

    assert [r["id"] for r in captured["telemetry"]] == [1, 2]
    assert missing == []
    assert path.exists()
    daily = tmp_path / "self_evolution" / "daily"
    leftovers = [p for p in daily.iterdir() if p.name.startswith("mirror-backfill-")]
    assert leftovers == [], f"temp dir not cleaned: {leftovers}"
    assert backfill_mod.gc.isenabled(), "cyclic GC must be re-enabled after backfill"


def test_backfill_restores_fetch_events_window(
    backfill_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """QA #1010 nit: backfill() replaces collect._fetch_events_window for the
    duration of collect() only — a same-process caller that collects again
    must go back to the original path, not silently read a consumed temp dir."""
    now = datetime.now(UTC)
    logs = tmp_path / "logs"
    logs.mkdir()
    for day in _window_days(now, 1):
        _write_mirror_day(logs, day, [])
    monkeypatch.setattr(backfill_mod, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(backfill_mod, "MIRROR_DIR", logs)
    monkeypatch.setattr(backfill_mod.collect, "collect", _no_records)
    sentinel = object()
    monkeypatch.setattr(backfill_mod.collect, "_fetch_events_window", sentinel)

    backfill_mod.backfill(1, "test-week")

    assert backfill_mod.collect._fetch_events_window is sentinel


def test_backfill_sweeps_stale_orphan_temp_dirs(
    backfill_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """QA #1010 nit: a SIGKILLed run leaves mirror-backfill-* staging dirs;
    backfill() sweeps orphans older than an hour so a crashed dense run does
    not leak hundreds of MB, while a live run's young dir is left alone."""
    now = datetime.now(UTC)
    logs = tmp_path / "logs"
    logs.mkdir()
    for day in _window_days(now, 1):
        _write_mirror_day(logs, day, [])
    monkeypatch.setattr(backfill_mod, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(backfill_mod, "MIRROR_DIR", logs)
    monkeypatch.setattr(backfill_mod.collect, "collect", _no_records)
    daily = tmp_path / "self_evolution" / "daily"
    daily.mkdir(parents=True)
    stale = daily / "mirror-backfill-deadbeef"
    stale.mkdir()
    (stale / "telemetry.jsonl").write_text("{}")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    fresh = daily / "mirror-backfill-cafebabe"
    fresh.mkdir()

    backfill_mod.backfill(1, "test-week")

    assert not stale.exists(), "stale orphan must be swept"
    assert fresh.exists(), "young dir must survive"
