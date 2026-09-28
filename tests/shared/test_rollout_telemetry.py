"""Deploy settle-hold telemetry — the one `[rollout-telemetry]` line a settle
hold's early release prints (task #1820 / C3 task #2189).
"""

from __future__ import annotations

import json

import pytest

from shared import rollout_telemetry as rt


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
