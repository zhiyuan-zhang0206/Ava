"""Refresh cadence through the CLI and real Git history, without running suites."""

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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


def _event(sha: str) -> dict[str, Any]:
    return {
        "action": "completed",
        "workflow_run": {
            "name": "CI",
            "path": ".github/workflows/ci.yml",
            "event": "push",
            "status": "completed",
            "conclusion": "success",
            "head_branch": "main",
            "head_repository": {"full_name": "ava/example"},
            "head_sha": sha,
            "id": 42,
            "updated_at": datetime.now(UTC).isoformat(),
        },
    }


def _plan(
    tmp_path: Path,
    event: dict[str, Any],
    *,
    trigger: str = "workflow_run",
    published: str | None = None,
) -> dict[str, str]:
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event))
    output = tmp_path / "output"
    output.unlink(missing_ok=True)
    args = [
        "plan",
        "--event",
        trigger,
        "--event-path",
        str(event_path),
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


def test_reuse_starts_at_twenty_changes_and_preserves_source_run(
    tmp_path: Path,
    history: list[str],
) -> None:
    before = _plan(tmp_path, _event(history[19]))
    assert before["mode"] == "skip"
    due = _plan(tmp_path, _event(history[20]))
    assert due["mode"] == "reuse"
    assert due["source-sha"] == history[20]
    assert due["source-run-id"] == "42"


def test_pending_publication_resets_cadence_and_supersedes_out_of_order_ci(
    tmp_path: Path,
    history: list[str],
) -> None:
    _git("checkout", "-b", "published")
    # A newer generation wins even when its long-running measurement started
    # before an older generation's publication.
    _stamp(history[20], datetime.now(UTC) - timedelta(hours=1))
    _git("add", ".test_durations.source.json")
    _git("commit", "-m", "publish new measurement for review")
    _git("checkout", "main")
    assert _plan(tmp_path, _event(history[19]), published="published")["mode"] == "skip"
    assert _plan(tmp_path, _event(history[22]), published="published")["mode"] == "skip"
    assert "Applied on main:" in (tmp_path / "summary").read_text()
    assert history[0] in (tmp_path / "summary").read_text()
    assert history[20] in (tmp_path / "summary").read_text()


def test_noop_does_not_record_freshness_and_daily_backstop_uses_real_measurement(
    tmp_path: Path,
    history: list[str],
) -> None:
    stamp_path = tmp_path / ".test_durations.source.json"
    original = stamp_path.read_bytes()
    assert _plan(tmp_path, {}, trigger="schedule")["mode"] == "skip"
    assert stamp_path.read_bytes() == original
    _stamp(history[0], datetime.now(UTC) - timedelta(hours=6))
    _git("add", ".test_durations.source.json")
    _git("commit", "-m", "older measurement")
    original = stamp_path.read_bytes()
    for _ in range(2):
        assert _plan(tmp_path, {}, trigger="schedule")["mode"] == "measure"
        assert stamp_path.read_bytes() == original


def test_manual_refresh_forces_measurement_and_legacy_cache_bootstraps(
    tmp_path: Path,
    history: list[str],
) -> None:
    assert _plan(tmp_path, {}, trigger="workflow_dispatch")["mode"] == "measure"
    _git("rm", ".test_durations.source.json")
    _git("commit", "-m", "legacy cache without measurement provenance")
    head = _git("rev-parse", "HEAD")
    assert _plan(tmp_path, _event(head))["mode"] == "reuse"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event", "pull_request"),
        ("head_branch", "feature"),
        ("conclusion", "failure"),
        ("status", "in_progress"),
        ("path", ".github/workflows/other.yml"),
        ("head_sha", "main"),
        ("id", True),
        ("updated_at", "2026-10-08T00:00:00"),
        ("head_repository", {"full_name": "fork/example"}),
    ],
)
def test_untrusted_or_invalid_ci_never_produces_a_refresh_plan(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    event = _event("a" * 40)
    event["workflow_run"][field] = value
    with pytest.raises((ValidationError, ValueError)):
        _plan(tmp_path, event)
    assert not (tmp_path / "output").exists()


def test_source_outside_main_and_corrupt_provenance_fail_before_outputs(
    tmp_path: Path,
    history: list[str],
) -> None:
    _git("checkout", "-b", "outside", history[0])
    _git("commit", "--allow-empty", "-m", "unmerged source")
    outside = _git("rev-parse", "HEAD")
    _git("checkout", "main")
    with pytest.raises(ValueError, match="outside"):
        _plan(tmp_path, _event(outside))
    _git("checkout", "-b", "invalid")
    (tmp_path / ".test_durations.source.json").write_text('{"schema_version":99}')
    _git("add", ".test_durations.source.json")
    _git("commit", "-m", "invalid provenance")
    _git("checkout", "main")
    with pytest.raises(ValidationError):
        _plan(tmp_path, _event(history[20]), published="invalid")
    assert not (tmp_path / "output").exists()
