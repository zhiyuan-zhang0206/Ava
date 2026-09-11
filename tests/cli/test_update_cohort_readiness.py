"""The read-only per-target cohort readiness report annotated onto --dry-run."""

from __future__ import annotations

import sys
from io import StringIO

import pytest

from cli.commands import _update_cohort as _cohort


def _row(
    agent_id: int,
    status: str,
    *,
    owner: bool = False,
    expired: bool = False,
    lifecycle: bool = False,
) -> _cohort.CohortRow:
    return _cohort.CohortRow(
        agent_id=agent_id,
        status=status,
        has_owner=owner,
        lease_expired=expired,
        lifecycle_pending=lifecycle,
    )


def _target(
    machine: str, *rows: _cohort.CohortRow, inclusion: str = "included"
) -> _cohort.TargetCohort:
    return _cohort.TargetCohort(machine=machine, inclusion=inclusion, rows=tuple(rows))


@pytest.mark.parametrize(
    ("status", "owner", "expired", "lifecycle", "expected"),
    [
        ("restarting", True, False, False, _cohort.RESTARTING),
        ("running", True, True, False, _cohort.STALE_LEASE),
        ("idling", True, False, False, _cohort.IDLE_HOSTED),
        ("idling", True, False, True, _cohort.IDLE_HOSTED),
        ("idling", False, False, False, _cohort.IDLE_UNHOSTED),
        ("idling", False, False, True, _cohort.EXEC_PENDING),
        ("running", True, False, True, _cohort.EXEC_PENDING),
        ("running", True, False, False, _cohort.RUNNING),
        ("running", False, False, False, _cohort.OTHER),
    ],
)
def test_classify_row_buckets(
    status: str, owner: bool, expired: bool, lifecycle: bool, expected: str
) -> None:
    row = _row(1, status, owner=owner, expired=expired, lifecycle=lifecycle)
    assert _cohort.classify_row(row) == expected


def test_report_marks_empty_cohorts_and_ready_verdict() -> None:
    lines = _cohort.report_lines([_target("wsl"), _target("company-air")])
    joined = "\n".join(lines)
    assert "wsl: cohort empty" in joined
    assert "company-air: cohort empty" in joined
    assert "verdict: ready" in joined


def test_report_flags_idle_hosted_as_held_wake_consumer() -> None:
    lines = _cohort.report_lines(
        [_target("macmini", _row(7, "idling", owner=True), _row(8, "running", owner=True))]
    )
    joined = "\n".join(lines)
    assert "⚠ macmini: 2 non-terminated — 1x idle-hosted, 1x running" in joined
    assert "1x idle-hosted: needs the held-wake path" in joined
    assert "attempt allowed" in joined
    assert "held-wake path (stalls on pre-fix runner code)" in joined


def test_report_blocks_on_restarting_and_stale_lease() -> None:
    lines = _cohort.report_lines(
        [
            _target(
                "company-mini",
                _row(11, "restarting"),
                _row(12, "running", owner=True, expired=True),
                _row(13, "running", owner=True),
            )
        ]
    )
    joined = "\n".join(lines)
    assert "✗ company-mini: 3 non-terminated" in joined
    assert "[2 blocking]" in joined
    assert "✗ #11 restarting" in joined
    assert "✗ #12 stale-lease" in joined
    assert "verdict: NOT ready — clear 2 blocking row(s) first" in joined


def test_report_lists_skipped_excluded_machines() -> None:
    lines = _cohort.report_lines([_target("company-air", inclusion="staging")])
    joined = "\n".join(lines)
    assert "(skipped: company-air (staging))" in joined
    assert "verdict: ready" in joined


def test_print_cohort_readiness_survives_database_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> list[_cohort.TargetCohort]:
        raise RuntimeError("db down")

    monkeypatch.setattr(_cohort, "collect_targets", boom)
    buf = StringIO()
    _cohort.print_cohort_readiness(stream=buf)
    out = buf.getvalue()
    assert "skipped (RuntimeError: db down)" in out


def test_dry_run_prints_readiness_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`cmd_update --dry-run` must annotate the run with the readiness report."""
    marker: list[str] = []

    def fake_readiness() -> None:
        marker.append("readiness")
        sys.stdout.write("READINESS-MARKER\n")

    class _Accepted:
        status_code = 202

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"session": "ava-rollout-dryrun", "log": "rollout-dry.log"}

    def fake_post(url: str, **kwargs: object) -> _Accepted:
        return _Accepted()

    monkeypatch.setattr(_cohort, "print_cohort_readiness", fake_readiness)
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr("httpx.post", fake_post)

    from cli.commands import cmd_update

    rc = cmd_update(dry_run=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert marker == ["readiness"]
    assert out.index("READINESS-MARKER") < out.index("dispatched")
