"""scripts/ci/shard_counts.py and the workflow steps that run it.

CI's shards run `pytest -q`, so nothing in a job log says which tests ran. The shard
step counts the JUnit report pytest already wrote, per tests/ directory; one light job adds
the shards up and compares the total with the previous main run's. These tests pin the
counting, the comparison and the wiring, and lock the assumption everything rests on:
with `-o junit_family=xunit1`, pytest writes each testcase's rootdir-relative `file`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from scripts.ci import shard_counts

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = yaml.safe_load((_REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
_JOBS = cast("dict[str, Any]", _WORKFLOW["jobs"])


def _junit(path: Path, *cases: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<?xml version="1.0"?><testsuites><testsuite name="pytest">'
        + "".join(cases)
        + "</testsuite></testsuites>",
        encoding="utf-8",
    )
    return path


def _case(file: str, name: str = "test_it", outcome: str = "") -> str:
    return f'<testcase classname="c" name="{name}" file="{file}">{outcome}</testcase>'


# ── counting ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("test_file", "bucket"),
    [
        ("tests/test_top.py", "tests"),
        ("tests/agent/test_loop.py", "tests/agent"),
        ("tests/agent/sub/deeper/test_x.py", "tests/agent"),
        ("base/packages/tests/test_a.py", "base/packages/tests"),
        ("base/packages/tests/area/test_b.py", "base/packages/tests"),
        (
            "ava_builtins/skills/gmail/scripts/tests/test_g.py",
            "ava_builtins/skills/gmail/scripts/tests",
        ),
        ("base/x/tests/tests/test_nested.py", "base/x/tests"),
    ],
)
def test_a_test_file_is_counted_under_its_tests_directory(test_file: str, bucket: str) -> None:
    assert shard_counts.bucket_of(test_file) == bucket


def test_a_test_outside_every_tests_directory_stops_the_count() -> None:
    with pytest.raises(SystemExit, match="outside every tests/ directory"):
        shard_counts.bucket_of("base/db/test_db_guard.py")


def test_junit_counts_tests_skips_and_failures_per_directory(tmp_path: Path) -> None:
    report = _junit(
        tmp_path / "r.xml",
        _case("tests/agent/test_a.py", "test_1"),
        _case("tests/agent/test_a.py", "test_2[x]", "<skipped/>"),
        _case("tests/agent/test_b.py", "test_3", "<failure/>"),
        _case("base/packages/tests/test_c.py", "test_4", "<error/>"),
    )
    assert shard_counts.count_junit(report) == {
        "tests": 4,
        "skipped": 1,
        "failed": 2,
        "buckets": {"tests/agent": 3, "base/packages/tests": 1},
    }


def test_a_testcase_without_a_file_attribute_stops_the_count(tmp_path: Path) -> None:
    report = _junit(tmp_path / "r.xml", '<testcase classname="c" name="t"/>')
    with pytest.raises(SystemExit, match="xunit1"):
        shard_counts.count_junit(report)


def test_real_pytest_writes_the_rootdir_relative_file_the_count_relies_on(tmp_path: Path) -> None:
    """Locks the assumption: a package's test and a top-level one, parametrized, under importlib."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = ["--import-mode=importlib"]\n', encoding="utf-8"
    )
    body = textwrap.dedent(
        """
        import pytest

        @pytest.mark.parametrize("n", [1, 2, 3])
        def test_it(n) -> None:
            pass
        """
    )
    for rel in ("tests/agent/test_a.py", "base/pkg/tests/area/test_b.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(body, encoding="utf-8")
    report = tmp_path / "tmp" / "junit-x.xml"
    subprocess.run(  # noqa: S603 - our own interpreter, synthetic paths
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junit-xml={report}",
            "-o",
            "junit_family=xunit1",
            "tests",
            "base",
        ],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    assert shard_counts.count_junit(report)["buckets"] == {"tests/agent": 3, "base/pkg/tests": 3}


# ── the shard step ──────────────────────────────────────────────────────────


def test_the_shard_step_counts_the_last_attempt_and_writes_log_summary_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = tmp_path / "summary.md"
    _junit(tmp_path / "tmp/junit-backend-shard-3-a1.xml", _case("tests/agent/test_a.py"))
    _junit(
        tmp_path / "tmp/junit-backend-shard-3-a2.xml",
        _case("tests/agent/test_a.py", "test_1"),
        _case("tests/agent/test_a.py", "test_2"),
        _case("base/packages/tests/test_c.py"),
    )
    out = tmp_path / "tmp/shard-counts-3.json"
    argv = [
        "shard",
        "--group",
        "3",
        "--junit",
        str(tmp_path / "tmp" / "junit-backend-shard-3-a*.xml"),
    ]
    assert shard_counts.main([*argv, "--out", str(out), "--summary", str(summary)]) == 0
    assert json.loads(out.read_text()) == {
        "group": "3",
        "tests": 3,
        "skipped": 0,
        "failed": 0,
        "buckets": {"tests/agent": 2, "base/packages/tests": 1},
    }
    log = capsys.readouterr().out
    assert "shard 3: 3 tests executed" in log
    assert "junit-backend-shard-3-a2.xml" in log
    assert "| tests/agent | 2 |" in summary.read_text()


def test_a_shard_with_no_junit_report_says_so(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="no JUnit report matches"):
        shard_counts.main(
            ["shard", "--group", "1", "--junit", f"{tmp_path}/none-*.xml", "--out", "x.json"]
        )


# ── the total ───────────────────────────────────────────────────────────────


def _counts(directory: Path, group: str, buckets: dict[str, int], skipped: int = 0) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "group": group,
        "tests": sum(buckets.values()),
        "skipped": skipped,
        "failed": 0,
        "buckets": buckets,
    }
    (directory / f"shard-counts-{group}.json").write_text(json.dumps(payload), encoding="utf-8")


def _baseline(path: Path, buckets: dict[str, int]) -> Path:
    payload = {"tests": sum(buckets.values()), "buckets": buckets, "sha": "a" * 40, "run_id": "77"}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_the_total_adds_the_shards_up_and_shows_what_moved_against_the_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    counts = tmp_path / "counts"
    _counts(counts, "1", {"tests/base": 100, "tests/agent": 50}, skipped=2)
    _counts(counts, "serial", {"tests/base": 10})
    baseline = _baseline(tmp_path / "b.json", {"tests/base": 210, "tests/agent": 50})
    out = tmp_path / "out.json"
    argv = ["total", "--dir", str(counts), "--expected", "1 serial", "--baseline", str(baseline)]
    summary = tmp_path / "summary.md"
    identity = ["--sha", "b" * 40, "--run-id", "88", "--summary", str(summary)]
    assert shard_counts.main([*argv, "--out", str(out), *identity]) == 0
    log = capsys.readouterr().out
    assert "CI executed 160 backend tests (2 skipped), from 2/2 count files" in log
    assert "against main aaaaaaaaa (run 77): 260 -> 160 (-100)" in log
    assert "tests/base" in log
    assert "-100" in log
    assert "tests/agent" not in log.split("against main")[1]  # unchanged directories are silent
    recorded = json.loads(out.read_text())
    assert recorded["buckets"] == {"tests/base": 110, "tests/agent": 50}
    assert (recorded["sha"], recorded["run_id"]) == ("b" * 40, "88")
    assert "against main aaaaaaaaa" in summary.read_text()
    assert "| tests/base | 110 |" in summary.read_text()


def test_a_moved_test_shows_as_one_directory_down_and_another_up(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    counts = tmp_path / "counts"
    _counts(counts, "1", {"tests/base": 88, "base/packages/tests": 12})
    baseline = _baseline(tmp_path / "b.json", {"tests/base": 100})
    argv = ["total", "--dir", str(counts), "--expected", "1", "--baseline", str(baseline)]
    shard_counts.main(argv)
    log = capsys.readouterr().out
    assert "100 -> 100 (+0)" in log
    assert "base/packages/tests" in log
    assert "+12" in log
    assert "-12" in log


def test_no_baseline_is_not_an_error_and_the_run_becomes_the_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    counts = tmp_path / "counts"
    _counts(counts, "1", {"tests/base": 5})
    out = tmp_path / "out.json"
    argv = [
        "total",
        "--dir",
        str(counts),
        "--expected",
        "1",
        "--baseline",
        str(tmp_path / "no.json"),
    ]
    assert shard_counts.main([*argv, "--out", str(out)]) == 0
    assert "no baseline" in capsys.readouterr().out
    assert json.loads(out.read_text())["tests"] == 5


def test_a_missing_count_file_marks_the_total_incomplete_and_records_no_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    counts = tmp_path / "counts"
    _counts(counts, "1", {"tests/base": 5})
    baseline = _baseline(tmp_path / "b.json", {"tests/base": 5})
    out = tmp_path / "out.json"
    argv = ["total", "--dir", str(counts), "--expected", "1 2", "--baseline", str(baseline)]
    assert shard_counts.main([*argv, "--out", str(out)]) == 0  # informational: never a failure
    log = capsys.readouterr().out
    assert "INCOMPLETE: no count file for 2" in log
    assert "against main" not in log
    assert not out.exists()


# ── the workflow wiring ─────────────────────────────────────────────────────


def _steps(job: str) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", _JOBS[job]["steps"])


def _index(steps: list[dict[str, Any]], name: str) -> int:
    matches = [i for i, step in enumerate(steps) if step.get("name") == name]
    assert len(matches) == 1, f"expected exactly one step named {name!r}"
    return matches[0]


@pytest.mark.parametrize(
    ("job", "group"), [("backend-shard", "${{ matrix.group }}"), ("backend-serial", "serial")]
)
def test_every_test_running_job_reports_and_uploads_its_counts(job: str, group: str) -> None:
    steps = _steps(job)
    report = steps[_index(steps, "Report executed test counts")]
    upload = steps[_index(steps, "Upload executed test counts")]
    assert report["continue-on-error"] is True
    assert upload["continue-on-error"] is True
    assert "scripts/ci/shard_counts.py shard" in report["run"]
    assert f"--group {group}" in report["run"]
    assert upload["with"]["name"] == f"shard-counts-{group}"
    # Right after the test step, ahead of the Trunk gate, whatever the tests' outcome.
    run_step = next(i for i, step in enumerate(steps) if "pytest" in str(step.get("run", "")))
    gate = _index(steps, "Trunk quarantine gate (upload test results)")
    assert run_step < _index(steps, "Report executed test counts") < gate
    assert report["if"] == upload["if"] == "${{ !cancelled() }}"


def test_the_shard_report_reads_the_junit_file_the_shard_writes() -> None:
    shard = _JOBS["backend-shard"]
    steps = shard["steps"]
    pytest_run = steps[_index(steps, "Run pytest shard")]["run"]
    report = steps[_index(steps, "Report executed test counts")]["run"]
    assert "--junit-xml=tmp/junit-backend-shard-${{ matrix.group }}-a1.xml" in pytest_run
    assert "-o junit_family=xunit1" in pytest_run
    assert "tmp/junit-backend-shard-${{ matrix.group }}-a*.xml" in report
    serial = _steps("backend-serial")
    serial_run = next(step["run"] for step in serial if "pytest -m flaky" in str(step.get("run")))
    assert "--junit-xml=tmp/junit-backend-serial.xml" in serial_run
    assert "-o junit_family=xunit1" in serial_run
    assert (
        "--junit tmp/junit-backend-serial.xml"
        in serial[_index(serial, "Report executed test counts")]["run"]
    )


def test_the_total_job_expects_exactly_the_shards_and_the_serial_bucket() -> None:
    job = _JOBS["backend-test-counts"]
    shard_groups = _JOBS["backend-shard"]["strategy"]["matrix"]["group"]
    assert job["env"]["COUNT_GROUPS"] == " ".join([*map(str, shard_groups), "serial"])


def test_the_total_job_is_informational_and_needs_only_read_access() -> None:
    job = _JOBS["backend-test-counts"]
    assert job["continue-on-error"] is True
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    assert set(job["needs"]) == {"classify", "test-select", "backend-shard", "backend-serial"}
    # The fan-out condition of backend-shard, without the always(): a cancelled run stops.
    assert job["if"] == _JOBS["backend-shard"]["if"].replace("always()", "!cancelled()")
    # Nothing waits on it: the required aggregator does not list it.
    assert "backend-test-counts" not in _JOBS["backend"]["needs"]
    for step in job["steps"]:
        assert step.get("continue-on-error") is True or step["uses"].startswith("actions/checkout")


def test_only_a_push_to_main_records_the_baseline() -> None:
    steps = _steps("backend-test-counts")
    record = steps[_index(steps, "Record the total as the next baseline")]
    assert record["if"] == (
        "${{ !cancelled() && github.event_name == 'push' && github.ref == 'refs/heads/main' }}"
    )
    assert record["with"]["name"] == "test-count-baseline"
    fetch = steps[_index(steps, "Fetch the previous main total")]["run"]
    assert "--name test-count-baseline" in fetch
    assert "--branch main" in fetch
