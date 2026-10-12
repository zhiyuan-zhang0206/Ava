"""Refresh cadence through the CLI and real Git history, without running suites."""

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts.ci import duration_refresh_policy as policy


def _git(*args: str) -> str:
    return subprocess.run(  # noqa: S603 — fixed git argv, test-owned temporary history
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _stamp(sha: str, measured_at: datetime) -> None:
    assert (
        policy.main(
            [
                "stamp",
                "--source-sha",
                sha,
                "--run-id",
                "7",
                "--measured-at",
                measured_at.isoformat(),
            ]
        )
        == 0
    )


@pytest.fixture
def history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.chdir(tmp_path)
    _git("init", "-b", "main")
    _git("config", "user.name", "Duration test")
    _git("config", "user.email", "duration@example.com")
    (tmp_path / ".test_durations").write_text('{"tests/test_example.py::test_one":1.0}\n')
    _git("add", ".test_durations")
    _git("commit", "-m", "initial timings")
    commits = [_git("rev-parse", "HEAD")]
    _stamp(commits[0], datetime.now(UTC))
    _git("add", ".test_durations.source.json")
    _git("commit", "-m", "publish measurement provenance")
    commits.append(_git("rev-parse", "HEAD"))
    for index in range(2, 23):
        _git("commit", "--allow-empty", "-m", f"main change {index}")
        commits.append(_git("rev-parse", "HEAD"))
    return commits


def _plan(
    tmp_path: Path,
    *,
    trigger: str = "schedule",
    published: str | None = None,
) -> dict[str, str]:
    output = tmp_path / "output"
    output.unlink(missing_ok=True)
    args = [
        "plan",
        "--event",
        trigger,
        "--repository",
        "ava/example",
        "--run-id",
        "43",
        "--output",
        str(output),
        "--summary",
        str(tmp_path / "summary"),
    ]
    if published:
        args.extend(["--published-ref", published])
    assert policy.main(args) == 0
    return dict(line.split("=", 1) for line in output.read_text().splitlines())


def test_measurement_source_is_the_checked_out_main_head_and_this_run(
    tmp_path: Path,
    history: list[str],
) -> None:
    _stamp(history[0], datetime.now(UTC) - timedelta(hours=6))
    _git("add", ".test_durations.source.json")
    _git("commit", "-m", "older measurement")
    due = _plan(tmp_path)
    assert due["mode"] == "measure"
    assert due["source-sha"] == _git("rev-parse", "HEAD")
    assert due["source-run-id"] == "43"


def test_pending_publication_suppresses_measurement_until_the_backstop_window_passes(
    tmp_path: Path,
    history: list[str],
) -> None:
    _git("checkout", "-b", "published")
    _stamp(history[20], datetime.now(UTC) - timedelta(hours=1))
    _git("add", ".test_durations.source.json")
    _git("commit", "-m", "publish new measurement for review")
    _git("checkout", "main")
    _stamp(history[0], datetime.now(UTC) - timedelta(hours=9))
    _git("add", ".test_durations.source.json")
    _git("commit", "-m", "older applied measurement")
    assert _plan(tmp_path, published="published")["mode"] == "skip"
    summary = (tmp_path / "summary").read_text()
    assert "Applied on main:" in summary
    assert history[0] in summary
    assert history[20] in summary
    assert _plan(tmp_path)["mode"] == "measure"


def test_noop_does_not_record_freshness_and_daily_backstop_uses_real_measurement(
    tmp_path: Path,
    history: list[str],
) -> None:
    stamp_path = tmp_path / ".test_durations.source.json"
    original = stamp_path.read_bytes()
    assert _plan(tmp_path)["mode"] == "skip"
    assert stamp_path.read_bytes() == original
    _stamp(history[0], datetime.now(UTC) - timedelta(hours=6))
    _git("add", ".test_durations.source.json")
    _git("commit", "-m", "older measurement")
    original = stamp_path.read_bytes()
    for _ in range(2):
        assert _plan(tmp_path)["mode"] == "measure"
        assert stamp_path.read_bytes() == original


def test_manual_refresh_forces_measurement_and_legacy_cache_bootstraps(
    tmp_path: Path,
    history: list[str],
) -> None:
    assert _plan(tmp_path, trigger="workflow_dispatch")["mode"] == "measure"
    _git("rm", ".test_durations.source.json")
    _git("commit", "-m", "legacy cache without measurement provenance")
    assert _plan(tmp_path)["mode"] == "measure"


def test_ci_is_not_a_refresh_trigger(tmp_path: Path, history: list[str]) -> None:
    with pytest.raises(SystemExit):
        _plan(tmp_path, trigger="workflow_run")
    assert not (tmp_path / "output").exists()


def test_corrupt_provenance_fails_before_outputs(
    tmp_path: Path,
    history: list[str],
) -> None:
    _git("checkout", "-b", "invalid")
    (tmp_path / ".test_durations.source.json").write_text('{"schema_version":99}')
    _git("add", ".test_durations.source.json")
    _git("commit", "-m", "invalid provenance")
    _git("checkout", "main")
    with pytest.raises(ValidationError):
        _plan(tmp_path, published="invalid")
    assert not (tmp_path / "output").exists()
