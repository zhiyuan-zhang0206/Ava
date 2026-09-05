"""Rollout phase telemetry — the numbers behind the 368s breakdown.

Task #1820 (user forensic ruling 2026-08-27): the migration-bearing rollout's
per-phase durations were reconstructed by hand afterwards. These cover the two
emitters every later rollout's log will carry — the per-stage lines and the
aggregate JSON summary — and the pairing rule the updater-log reader depends on
(`ops.updater_outcome` parses the `[updater] stage=` lines; this file pins the
emitter side of that contract).
"""

from __future__ import annotations

import json

import pytest

from shared import rollout_telemetry as rt


def test_stage_records_into_the_active_collector_and_prints_a_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A gateway phase under the ambient collector both prints its own line the
    moment it ends (a killed rollout still shows completed stages) and lands in
    the aggregate."""
    collector = rt.activate()
    try:
        with rt.stage("phase0_fetch"):
            pass
    finally:
        rt.deactivate()

    out = capsys.readouterr().out
    assert "[rollout-telemetry] stage=phase0_fetch dur=" in out
    assert collector.summary()["stages"] == {"phase0_fetch": 0.0}


def test_stage_prints_even_without_a_collector(capsys: pytest.CaptureFixture[str]) -> None:
    """`stage()` is a pure printer outside an orchestration (a rollback, a
    frontend-only fast path) — observability must not depend on the collector
    being wired."""
    with rt.stage("frontend_build"):
        pass
    assert "[rollout-telemetry] stage=frontend_build dur=" in capsys.readouterr().out


def test_updater_stage_prints_the_lines_the_log_reader_parses(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The updater-side line shape is a contract with
    `ops.updater_outcome._STAGE_LINE_RE`: an emitter that printed a shape the
    reader does not recognise would be a marker nothing reads, and that failure
    is silent. Two lines now — the entry `t=` (the in-flight evidence the
    no-progress judgment reads, P1 2026-08-30) and the exit `dur=`."""
    with rt.updater_stage("uv_sync"):
        pass
    entry, exit_ = capsys.readouterr().out.strip().splitlines()
    assert entry.startswith("[updater] stage=uv_sync t=")
    assert exit_.startswith("[updater] stage=uv_sync dur=")
    assert exit_.endswith("s")


def test_nested_stages_record_beside_the_outer_stage() -> None:
    """The local leg's stop/checkout/uv/start record beside the `local_leg` total
    that contains them — the summary carries both views."""
    collector = rt.activate()
    try:
        with rt.stage("local_leg"):
            with rt.stage("checkout"):
                pass
            with rt.stage("uv_sync"):
                pass
    finally:
        rt.deactivate()

    assert collector.summary()["stages"] == {"local_leg": 0.0, "checkout": 0.0, "uv_sync": 0.0}


def test_bytes_and_hosts_land_in_the_summary() -> None:
    collector = rt.activate()
    try:
        rt.record_bytes("snapshot", 4_567_030_217)
        rt.record_host("win", {"uv": 40.1, "stop": 2.3})
    finally:
        rt.deactivate()

    summary = collector.summary()
    assert summary["bytes"] == {"snapshot": 4_567_030_217}
    assert summary["hosts"] == {"win": {"uv": 40.1, "stop": 2.3}}


def test_summary_reports_prepare_details_and_observed_gateway_downtime() -> None:
    """The final JSON preserves its existing stage map while exposing the two
    user-visible rollout windows and additive prepare-check timings."""
    collector = rt.activate()
    try:
        collector.record("stop_the_world", 8.0)
        collector.record("local_leg", 30.0)
        collector.record("readiness", 2.0)
        rt.record_detail("prepare_checks", "staging_venv_s", 44.44)
    finally:
        rt.deactivate()

    summary = collector.summary()
    assert summary["gateway_downtime_s"] == 40.0
    assert summary["details"] == {"prepare_checks": {"staging_venv_s": 44.4}}


def test_record_host_ignores_empty_stages() -> None:
    collector = rt.activate()
    try:
        rt.record_host("wsl", {})
    finally:
        rt.deactivate()
    assert collector.summary()["hosts"] == {}


def test_print_summary_emits_one_parseable_json_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    collector = rt.activate()
    try:
        with rt.stage("preflight"):
            pass
        rt.record_bytes("snapshot", 123)
    finally:
        rt.deactivate()
    collector.print_summary()

    lines = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("[rollout-telemetry] ")
    ]
    assert len(lines) == 2  # the per-stage line + the summary
    payload = json.loads(lines[1].removeprefix("[rollout-telemetry] "))
    assert "preflight" in payload["stages"]
    assert payload["bytes"] == {"snapshot": 123}
    assert isinstance(payload["total_s"], float)


def test_settle_ended_prints_one_parseable_json_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The settle phase's own JSON line (C3, task #2189): the hold outlives the
    orchestration process, so its duration cannot ride the orchestration's
    aggregate summary — this is the line the rollout log ends with when an early
    release happens."""
    rt.settle_ended(dur_s=123.4, hosts=["wsl", "win"])

    lines = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("[rollout-telemetry] ")
    ]
    assert len(lines) == 1
    payload = json.loads(lines[0].removeprefix("[rollout-telemetry] "))
    # Hosts are sorted in the line, matching the settle note's ordering convention.
    assert payload == {"settle": {"dur_s": 123.4, "hosts": ["win", "wsl"]}}


def test_settle_ended_reports_an_unknowable_duration_as_null(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A settle hold that predates the settle_started_at column has no computable
    duration — reported as null, never guessed, so a reader never mistakes it for
    a zero-second settle."""
    rt.settle_ended(dur_s=None, hosts=["wsl"])
    out = capsys.readouterr().out
    assert '"dur_s": null' in out
    assert '"hosts": ["wsl"]' in out


def test_deactivate_drops_the_ambient_collector() -> None:
    rt.activate()
    rt.deactivate()
    with rt.stage("preflight"):
        pass
    # No collector: the stage printed but nothing was aggregated (nothing to
    # assert on except that no other call raises).
    assert rt._active.value is None


def test_rollout_telemetry_record_rounds_to_tenths() -> None:
    collector = rt.RolloutTelemetry()
    collector.record("phase_b", 77.1234)
    assert collector.summary()["stages"] == {"phase_b": pytest.approx(77.1)}  # pyright: ignore[reportUnknownMemberType]
