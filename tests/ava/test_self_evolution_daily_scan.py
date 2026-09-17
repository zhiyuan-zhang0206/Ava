"""Unit tests for the self-evolution daily scan (reference/daily_scan.py).

The reference scripts are standalone (the skill dir has a hyphen, so they are
not importable as a package); the test adds the reference dir to sys.path and
imports the module directly. `collect` is stubbed — no DB, no filesystem.
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
def daily_scan() -> Any:
    """The module under test, imported with its reference dir on sys.path.

    importlib (not a static import) so pyright does not try to resolve the
    reference dir at analysis time; the cast keeps the module's member types
    unknown-but-Any instead of erroring on them.
    """
    sys.path.insert(0, str(REF_DIR))
    try:
        return cast(Any, importlib.import_module("daily_scan"))
    finally:
        sys.path.remove(str(REF_DIR))


def _record(label: str, agent_id: int = 1, **overrides: object) -> dict[str, object]:
    rec: dict[str, object] = {
        "agent_id": agent_id,
        "week": "2026-08-13",
        "spawner": "user",
        "task_prompt": "",
        "followup_prompts": [],
        "corrections": [],
        "peer_feedback": [],
        "transcript": [],
        "final_output": "done",
        "turns": 3,
        "exec_failed": 0,
        "last_exec_failed": False,
        "compactions": 0,
        "breached": False,
        "terminated": False,
        "label": label,
    }
    rec.update(overrides)
    return rec


def test_alert_exit_is_2_when_any_run_is_bad(daily_scan: Any) -> None:
    ds = daily_scan
    assert ds.alert_exit([_record("ok"), _record("ok")]) == 0
    assert ds.alert_exit([_record("ok"), _record("fumbled")]) == 2
    assert ds.alert_exit([_record("failed")]) == 2


def test_alert_exit_is_2_when_no_runs_collected(daily_scan: Any) -> None:
    """An empty dataset means the data source broke — never "nothing to act
    on" (2026-08-14: PG events froze and the scan green-lit an empty day)."""
    ds = daily_scan
    assert ds.alert_exit([]) == 2
    rendered = ds.render([], Path("daily-2026-08-14.jsonl"), 1)
    assert "0 runs collected" in rendered


def test_render_lists_bad_runs_with_their_signals(daily_scan: Any) -> None:
    ds = daily_scan
    records = [
        _record("ok", agent_id=1),
        _record(
            "failed",
            agent_id=2,
            task_prompt="Fix the suite",
            corrections=["\u4e0d\u5bf9\uff0c\u91cd\u505a"],
            exec_failed=4,
            last_exec_failed=True,
        ),
    ]
    out = ds.render(records, Path("daily.jsonl"), 1)
    assert "runs: 2 (ok 1 / fumbled 0 / failed 1)" in out
    assert "ALERT — 1 run(s) worth mining:" in out
    assert "#2 failed" in out
    assert "1 user correction(s)" in out
    assert "4 failed exec(s)" in out
    assert "last exec failed" in out
    assert "Fix the suite" in out


def test_render_is_quiet_on_clean_day(daily_scan: Any) -> None:
    ds = daily_scan
    out = ds.render([_record("ok", agent_id=1), _record("ok", agent_id=2)], Path("d.jsonl"), 1)
    assert "ALERT" not in out


def test_render_reports_subprocess_and_shell_run_totals(daily_scan: Any) -> None:
    out = daily_scan.render(
        [
            _record("ok", subprocess_calls=3, tools_called={"ava.shell.run": 5}),
            _record("ok", agent_id=2, subprocess_calls=4, tools_called={"ava.shell.run": 6}),
        ],
        Path("daily.jsonl"),
        1,
    )

    assert "subprocess calls: 7 (shell.run 11)" in out.splitlines()


def test_scan_passes_include_test_flag_through(
    daily_scan: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--include-test` is measurement-only: the nightly scan must stay on the
    default (exclude), and the flag must reach collect untouched."""
    calls: list[tuple[object, ...]] = []

    class _CollectStub:
        @staticmethod
        def collect_with_counts(
            days: int, week: str | None, include_test: bool = False
        ) -> tuple[list[dict], dict[str, int]]:
            calls.append((days, week, include_test))
            return [], {"seen": 0, "excluded_test": 0, "skipped_meta": 0}

    monkeypatch.setattr(daily_scan, "collect", _CollectStub)
    monkeypatch.setattr(daily_scan, "ava_home", lambda: tmp_path)

    result1 = daily_scan.scan(1, week="w1")
    result2 = daily_scan.scan(2, week="w2", include_test=True)

    assert calls == [(1, "w1", False), (2, "w2", True)]
    assert result1.counts == {"seen": 0, "excluded_test": 0, "skipped_meta": 0}
    assert result2.counts == {"seen": 0, "excluded_test": 0, "skipped_meta": 0}
    # the normal path carries no fallback provenance
    assert result1.source_note is None and result1.missing_days == ()
    # the default run wrote the dataset file, the measurement run too
    assert (tmp_path / "self_evolution" / "daily").is_dir()


def test_alert_exit_is_0_on_test_only_window(daily_scan: Any) -> None:
    """A window whose only activity was TEST- spawns is a quiet day, not a
    data-source outage: the filter removed every run by design, so the
    empty-dataset sentinel must not fire (QA review of PR #698)."""
    ds = daily_scan
    counts = {"seen": 3, "excluded_test": 3, "skipped_meta": 0}
    assert ds.alert_exit([], counts) == 0
    rendered = ds.render([], Path("daily-test-only.jsonl"), 1, counts)
    assert "ALERT" not in rendered
    assert "0 runs collected" not in rendered
    assert "3 window agent(s) were TEST- spawns" in rendered


def test_alert_exit_stays_2_on_zero_seen_runs(daily_scan: Any) -> None:
    """The `seen > 0` guard is load-bearing: a truly empty source
    (seen=0 — the 2026-08-14 outage shape) must stay ALERT, never read as
    a TEST- only window (0 == 0 without the guard)."""
    ds = daily_scan
    assert ds.alert_exit([], {"seen": 0, "excluded_test": 0, "skipped_meta": 0}) == 2
    rendered = ds.render([], Path("d.jsonl"), 1, {"seen": 0, "excluded_test": 0, "skipped_meta": 0})
    assert "ALERT — 0 runs collected" in rendered


def test_alert_exit_stays_2_when_seen_runs_vanish_without_test_filter(
    daily_scan: Any,
) -> None:
    """The sentinel keys on pre-filter activity: a window whose seen runs
    produced no records for any other reason (e.g. every lifecycle row
    missing) is still an anomaly, never a quiet day."""
    ds = daily_scan
    counts = {"seen": 2, "excluded_test": 0, "skipped_meta": 2}
    assert ds.alert_exit([], counts) == 2
    rendered = ds.render([], Path("d.jsonl"), 1, counts)
    assert "ALERT — 0 runs collected" in rendered


def test_render_compacts_oversized_bad_list_when_report_is_persisted(daily_scan: Any) -> None:
    """A bad list over the stdout budget (the runner delivers only the output
    tail) collapses to counter lines once the full report is on disk, so the
    trailing summary + pointer always survive in the delivered tail."""
    ds = daily_scan
    records = [
        _record(
            "fumbled", agent_id=i, task_prompt=f"long task {i} " + "x" * 140, corrections=["fix"]
        )
        for i in range(1, 21)
    ]
    out = ds.render(records, Path("d.jsonl"), 1, report_path=Path("d.report.txt"), compact_bad=True)
    assert "(compact; details in report)" in out
    assert "| task:" not in out
    assert "  #1 fumbled c1" in out
    assert "full report: d.report.txt" in out
    assert len(out) < 2000


def test_render_keeps_rich_bad_lines_within_budget(daily_scan: Any) -> None:
    """A small bad list stays rich even under compact_bad: compaction is a
    fallback for oversized lists only."""
    ds = daily_scan
    records = [
        _record("fumbled", agent_id=1, task_prompt="short task"),
        _record("fumbled", agent_id=2, task_prompt="another task"),
        _record("failed", agent_id=3, task_prompt="third task"),
    ]
    out = ds.render(records, Path("d.jsonl"), 1, report_path=Path("d.report.txt"), compact_bad=True)
    assert "(compact" not in out
    assert "| task: short task" in out
    assert "full report: d.report.txt" in out


def test_render_appends_summary_and_report_pointer_last(daily_scan: Any) -> None:
    """The closing two lines carry the window summary and the persisted
    report path — the part tail truncation must keep."""
    ds = daily_scan
    records = [
        _record("ok", agent_id=1),
        _record("fumbled", agent_id=2, corrections=["a", "b"], peer_feedback=["p"]),
        _record("failed", agent_id=3, exec_failed=2, breached=True),
    ]
    out = ds.render(records, Path("d.jsonl"), 1, report_path=Path("d.report.txt"))
    lines = out.splitlines()
    assert lines[-2] == (
        "summary: 3 runs (ok 1 / fumbled 1 / failed 1) | corrections 2 | peer 1 "
        "| breached 1 | exec-fail runs 1"
    )
    assert lines[-1] == "full report: d.report.txt"


def test_render_defaults_unchanged(daily_scan: Any) -> None:
    """No kwargs keeps the pre-existing contract: no summary, no pointer, no
    compaction even for an oversized bad list."""
    ds = daily_scan
    records = [
        _record("fumbled", agent_id=i, task_prompt=f"long task {i} " + "x" * 140)
        for i in range(1, 21)
    ]
    out = ds.render(records, Path("d.jsonl"), 1)
    assert "summary:" not in out
    assert "full report:" not in out
    assert "(compact" not in out
    assert "| task:" in out


def test_render_never_compacts_without_report_path(daily_scan: Any) -> None:
    """Compacting without a stored report would drop the only copy of the
    task text, so the gate is report_path — compact_bad alone is not enough."""
    ds = daily_scan
    records = [
        _record("fumbled", agent_id=i, task_prompt=f"long task {i} " + "x" * 140)
        for i in range(1, 21)
    ]
    out = ds.render(records, Path("d.jsonl"), 1, compact_bad=True)
    assert "(compact" not in out
    assert "| task:" in out


def test_main_persists_report_and_prints_pointer(
    daily_scan: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """main() writes the rich report beside the dataset and prints the compact
    stdout view ending in the pointer line."""
    ds = daily_scan
    records = [
        _record("fumbled", agent_id=1, task_prompt="t" * 200, corrections=["a"]),
        _record("ok", agent_id=2),
    ]
    dataset = tmp_path / "daily.jsonl"

    def _fake_scan(days: int, include_test: bool = False) -> Any:
        assert days == 1
        assert include_test is False
        return ds.ScanResult(records, dataset, None)

    monkeypatch.setattr(ds, "scan", _fake_scan)
    monkeypatch.setattr(sys, "argv", ["daily_scan.py"])

    with pytest.raises(SystemExit) as excinfo:
        ds.main()

    assert excinfo.value.code == 2
    report = tmp_path / "daily.report.txt"
    # line 0 is the wall-clock header; the rest of the report must equal the
    # rich render
    assert (
        report.read_text(encoding="utf-8").splitlines()[1:]
        == ds.render(records, dataset, 1).splitlines()[1:]
    )
    assert not (tmp_path / "daily.report.txt.tmp").exists()  # tmp renamed away, not left behind
    out = capsys.readouterr().out
    assert f"full report: {report}" in out


def test_main_degrades_to_rich_output_when_report_unwritable(
    daily_scan: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed report publish (tmp + rename) must not compact stdout: the
    rich lines are then the only copy of the details."""
    ds = daily_scan
    records = [
        _record("fumbled", agent_id=i, task_prompt=f"long task {i} " + "x" * 140)
        for i in range(1, 21)
    ]
    dataset = tmp_path / "daily.jsonl"
    (tmp_path / "daily.report.txt").mkdir()  # the final replace() onto a directory raises OSError

    def _fake_scan(days: int, include_test: bool = False) -> Any:
        assert days == 1
        assert include_test is False
        return ds.ScanResult(records, dataset, None)

    monkeypatch.setattr(ds, "scan", _fake_scan)
    monkeypatch.setattr(sys, "argv", ["daily_scan.py"])

    with pytest.raises(SystemExit):
        ds.main()

    captured = capsys.readouterr()
    assert "could not write full report" in captured.err
    assert "full report: " not in captured.out
    assert "| task:" in captured.out


def test_compact_bad_line_pins_every_signal_token(daily_scan: Any) -> None:
    """The compact tokens mirror _why() one-for-one — pin the full mapping so
    a dropped or renamed signal cannot pass unnoticed (QA review of #2366)."""
    ds = daily_scan
    all_signals = _record(
        "fumbled",
        agent_id=7,
        followup_prompts=["a", "b"],
        corrections=["c"],
        peer_feedback=["p"],
        exec_failed=2,
        last_exec_failed=True,
        compactions=3,
        breached=True,
        terminated=True,
        final_output="",
        turns=1,
    )
    assert ds._compact_bad_line(all_signals) == "  #7 fumbled rep2 c1 pf1 ef2 last cp3 br term"
    assert ds._compact_bad_line(_record("failed", agent_id=8)) == "  #8 failed -"


# ── no-observability fallback (2026-09-17) ──────────────────────────────────


def test_scan_falls_back_to_mirror_on_no_observability_refusal(
    daily_scan: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A cluster without observability refuses /api/events reads (503
    observability_read_unavailable) — a policy state, not an outage. The scan
    must collect the window from the local mirror instead of failing rc=1
    every night, and carry the provenance on the result so the report and the
    schedule wake message cannot present mirror data as the live read."""
    ds = daily_scan
    records = [_record("ok", agent_id=1)]
    counts = {"seen": 1, "excluded_test": 0, "skipped_meta": 0}

    def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise ds.collect.ObservabilityReadUnavailable(
            "observability reads unavailable for this cluster"
        )

    calls: list[dict[str, Any]] = []

    def _from_mirror(days: int, week: str, *, include_test: bool = False) -> Any:
        calls.append({"days": days, "week": week, "include_test": include_test})
        return records, counts, ["20260916"]

    monkeypatch.setattr(ds.collect, "collect_with_counts", _refuse)
    monkeypatch.setattr(ds.mirror_backfill, "collect_from_mirror", _from_mirror)
    monkeypatch.setattr(ds, "ava_home", lambda: tmp_path)

    result = ds.scan(1, week="w1")

    assert result.records == records
    assert result.counts == counts
    assert calls == [{"days": 1, "week": "w1", "include_test": False}]
    assert result.missing_days == ("20260916",)
    assert result.source_note == (
        "local event mirror (no observability on this cluster); "
        "mirror file missing for 20260916 — window may be partial"
    )
    assert result.path == tmp_path / "self_evolution" / "daily" / "w1.jsonl"
    assert result.path.exists()  # the scan still owns the dataset write
    assert "collecting from the local event mirror" in capsys.readouterr().out


def test_scan_does_not_fall_back_on_a_transient_failure(
    daily_scan: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fallback keys on the no-observability type only: a transient
    /api/events failure stays loud (a silent switch to mirror rows could mask
    an outage), exactly as before."""
    ds = daily_scan
    called: list[str] = []

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("/api/events read failed after 5 attempts: HTTP 502")

    def _from_mirror(*_args: Any, **_kwargs: Any) -> Any:
        called.append("mirror")
        return [], {}, []

    monkeypatch.setattr(ds.collect, "collect_with_counts", _boom)
    monkeypatch.setattr(ds.mirror_backfill, "collect_from_mirror", _from_mirror)
    monkeypatch.setattr(ds, "ava_home", lambda: tmp_path)

    with pytest.raises(RuntimeError, match="after 5 attempts"):
        ds.scan(1, week="w1")

    assert called == []


def test_alert_exit_alerts_on_a_partial_fallback_window(daily_scan: Any) -> None:
    """A fallback window missing a mirror day is partial: it alerts even when
    every collected run is ok — the gap itself could hide bad runs
    (mirror_backfill's iron rule: never a silent partial dataset)."""
    ds = daily_scan
    ok = [_record("ok", agent_id=1)]
    counts = {"seen": 1, "excluded_test": 0, "skipped_meta": 0}

    assert ds.alert_exit(ok, counts, missing_days=("20260916",)) == 2
    assert ds.alert_exit(ok, counts, missing_days=()) == 0


def test_render_reports_the_fallback_source_before_the_closing_block(
    daily_scan: Any,
) -> None:
    """The source line lands in the stable tail (before summary/pointer), so
    the runner's tail crop keeps the provenance; the normal path has none."""
    ds = daily_scan
    records = [_record("ok", agent_id=1)]
    note = "local event mirror (no observability on this cluster)"

    out = ds.render(records, Path("d.jsonl"), 1, report_path=Path("d.report.txt"), source_note=note)
    lines = out.splitlines()
    assert f"source: {note}" in lines
    assert lines.index(f"source: {note}") < lines.index(
        "summary: 1 runs (ok 1 / fumbled 0 / failed 0) | corrections 0 | peer 0 "
        "| breached 0 | exec-fail runs 0"
    )

    plain = ds.render(records, Path("d.jsonl"), 1, report_path=Path("d.report.txt"))
    assert not any(line.startswith("source: ") for line in plain.splitlines())


def test_main_alerts_and_prints_the_source_line_on_a_partial_fallback(
    daily_scan: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """main() must thread the scan's provenance and missing days through:
    rc=2 on the partial window, and the source line in the printed tail."""
    ds = daily_scan
    records = [_record("ok", agent_id=1)]
    dataset = tmp_path / "daily.jsonl"
    note = "local event mirror (no observability on this cluster)"

    def _fake_scan(days: int, include_test: bool = False) -> Any:
        return ds.ScanResult(
            records,
            dataset,
            {"seen": 1, "excluded_test": 0, "skipped_meta": 0},
            note,
            ("20260916",),
        )

    monkeypatch.setattr(ds, "scan", _fake_scan)
    monkeypatch.setattr(sys, "argv", ["daily_scan.py"])

    with pytest.raises(SystemExit) as excinfo:
        ds.main()

    assert excinfo.value.code == 2  # partial window alerts even though runs are ok
    out = capsys.readouterr().out
    assert f"source: {note}" in out
    assert "full report:" in out
