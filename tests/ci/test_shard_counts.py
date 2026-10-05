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
            "ava_builtins/skills/integrations/gmail/scripts/tests/test_g.py",
            "ava_builtins/skills/integrations/gmail/scripts/tests",
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


def test_a_shard_that_executed_too_few_tests_fails_after_writing_its_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Zero tests is what a broken `testpaths` or a deselecting marker produces, and the
    uploader reads it as a green run."""
    _junit(tmp_path / "junit-1-a1.xml")
    out = tmp_path / "c.json"
    argv = ["shard", "--group", "1", "--junit", str(tmp_path / "junit-1-a*.xml"), "--out", str(out)]
    with pytest.raises(SystemExit, match="executed 0 tests, fewer than the 1 it must"):
        shard_counts.main([*argv, "--min-tests", "1"])
    assert json.loads(out.read_text())["tests"] == 0  # the counts are still there to read
    assert "shard 1: 0 tests executed" in capsys.readouterr().out
    assert shard_counts.main(argv) == 0  # no floor: an empty bucket is fine
    _junit(tmp_path / "junit-1-a2.xml", _case("tests/agent/test_a.py"))
    assert shard_counts.main([*argv, "--min-tests", "1"]) == 0


def test_a_testcase_without_a_file_names_itself(tmp_path: Path) -> None:
    """pytest's internal-error entry is `classname="pytest" name="internal"`, with no file."""
    report = _junit(
        tmp_path / "r.xml", '<testcase classname="pytest" name="internal"><error/></testcase>'
    )
    with pytest.raises(SystemExit, match=r"testcase pytest\.internal has no `file` attribute"):
        shard_counts.count_junit(report)


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
    # The report is a gate (see test_backend_test_gate.py); only the artifact upload is transport.
    assert "continue-on-error" not in report
    assert upload["continue-on-error"] is True
    assert "scripts/ci/shard_counts.py shard" in report["run"]
    assert f"--group {group}" in report["run"]
    assert upload["with"]["name"] == f"shard-counts-{group}"
    # After the test step, ahead of the native JUnit validator, for every outcome.
    run_step = next(i for i, step in enumerate(steps) if "pytest" in str(step.get("run", "")))
    gate = _index(steps, "Validate native test results")
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


# ── the leak guard's findings ───────────────────────────────────────────────


def _guarded(file: str, name: str, *props: tuple[str, str]) -> str:
    """A testcase carrying the leak guard's JUnit properties."""
    body = "".join(f'<property name="{key}" value="{value}"/>' for key, value in props)
    return _case(file, name, f"<properties>{body}</properties>")


def _leaks_in(directory: Path, group: str, *cases: str) -> None:
    """A shard count file written by the real counter from a JUnit report holding `cases`."""
    directory.mkdir(parents=True, exist_ok=True)
    report = _junit(directory / f"junit-{group}.xml", *cases)
    counts = {"group": group, **shard_counts.count_junit(report)}
    (directory / f"shard-counts-{group}.json").write_text(json.dumps(counts), encoding="utf-8")
    report.unlink()


def _annotation(log: str) -> list[str]:
    """The lines of the one `leak guard` annotation in `log`, decoded."""
    commands = [x for x in log.splitlines() if x.startswith("::warning title=leak guard::")]
    assert len(commands) == 1, "one annotation, on one line"
    return commands[0].split("::", 2)[2].replace("%0A", "\n").replace("%25", "%").split("\n")


def test_the_guards_properties_are_counted_with_the_test_that_carries_them(tmp_path: Path) -> None:
    report = _junit(
        tmp_path / "r.xml",
        _guarded("tests/a/test_x.py", "test_1", ("leak_guard", "env: AVA_X was added")),
        _guarded("tests/a/test_x.py", "test_2[p]", ("leak_guard_note", "sys.path: added ['/p']")),
        _guarded("tests/a/test_y.py", "test_3", ("leak_guard_fault", "snapshot: KeyError: 'x'")),
        _case("tests/a/test_y.py", "test_4"),
    )
    counted = shard_counts.count_junit(report)
    assert counted["tests"] == 4
    assert counted["leaks"] == [
        {"test": "tests/a/test_x.py::test_1", "kind": "env", "detail": "AVA_X was added"}
    ]
    assert counted["notes"] == [
        {"test": "tests/a/test_x.py::test_2[p]", "kind": "sys.path", "detail": "added ['/p']"}
    ]
    assert counted["faults"] == [
        {"test": "tests/a/test_y.py::test_3", "kind": "snapshot", "detail": "KeyError: 'x'"}
    ]


def test_a_property_of_any_shape_never_breaks_the_count(tmp_path: Path) -> None:
    """The shard step is a gate: whatever the guard wrote, counting the tests must still succeed."""
    report = _junit(
        tmp_path / "r.xml",
        _case(
            "tests/a/test_x.py", "test_1", '<properties><property name="leak_guard"/></properties>'
        ),
        _guarded("tests/a/test_x.py", "test_2", ("leak_guard", "no separator at all")),
        _guarded("tests/a/test_x.py", "test_3", ("leak_guard", ": leading separator")),
        _guarded("tests/a/test_x.py", "test_4", ("somebody_else", "env: X was added")),
    )
    counted = shard_counts.count_junit(report)
    assert counted["tests"] == 4
    assert [x["kind"] for x in counted["leaks"]] == ["", "no separator at all", ""]
    assert "notes" not in counted


def test_the_shard_step_reports_the_findings_and_still_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _junit(
        tmp_path / "junit-1-a1.xml",
        _guarded("tests/a/test_x.py", "test_1", ("leak_guard", "env: AVA_X was added")),
        _guarded("tests/a/test_x.py", "test_2", ("leak_guard_fault", "compare: OSError: gone")),
    )
    out = tmp_path / "c.json"
    argv = ["shard", "--group", "1", "--junit", str(tmp_path / "junit-1-a*.xml"), "--out", str(out)]
    assert shard_counts.main([*argv, "--min-tests", "1"]) == 0
    assert "leak guard: 1 leak finding(s) in 1 test(s), 1 guard fault(s)" in capsys.readouterr().out
    written = json.loads(out.read_text())
    assert written["leaks"][0]["test"] == "tests/a/test_x.py::test_1"
    assert written["faults"][0]["kind"] == "compare"


def test_a_clean_total_says_so_and_raises_no_annotation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    counts = tmp_path / "counts"
    _counts(counts, "1", {"tests/base": 5})
    report = tmp_path / "leaks.json"
    argv = ["total", "--dir", str(counts), "--expected", "1", "--leak-report", str(report)]
    assert shard_counts.main([*argv, "--sha", "c" * 40, "--run-id", "9"]) == 0
    log = capsys.readouterr().out
    assert "leak guard: 0 leak finding(s) in 0 test(s), 0 sys.path note(s)" in log
    assert "from 1/1 count files" in log
    assert "::warning" not in log
    written = json.loads(report.read_text())
    assert (written["complete"], written["leaks"], written["sha"], written["run_id"]) == (
        True,
        [],
        "c" * 40,
        "9",
    )


def test_the_total_raises_one_annotation_with_a_line_per_file_kind_and_thing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    counts = tmp_path / "counts"
    env = ("leak_guard", "env: AVA_X was added")
    signal = ("leak_guard", "signal: SIGINT handler SIG_DFL -> handler")
    _leaks_in(
        counts,
        "1",
        _guarded("tests/a/test_x.py", "test_1", env),
        _guarded("tests/a/test_x.py", "test_2", env),  # the same key again: one line, x2
        _guarded("tests/a/test_a.py", "test_3", signal),
        _guarded("tests/z/test_z.py", "test_4", ("leak_guard", "cwd: /a -> /b")),
    )
    _leaks_in(counts, "2", _guarded("tests/a/test_x.py", "test_5", env))
    _counts(counts, "3", {"tests/base": 1})
    summary = tmp_path / "summary.md"
    report = tmp_path / "leaks.json"
    argv = ["total", "--dir", str(counts), "--expected", "1 2 3 4"]
    assert shard_counts.main([*argv, "--summary", str(summary), "--leak-report", str(report)]) == 0
    lines = _annotation(capsys.readouterr().out)
    assert lines[0].startswith("leak guard: 5 leak finding(s) in 5 test(s)")
    assert "from 3/4 count files (INCOMPLETE: no file for 4)" in lines[0]
    assert lines[1] == "  by kind: cwd=1, env=3, signal=1"
    assert lines[2:] == [  # sorted by file, then kind, then thing
        "  tests/a/test_a.py  [signal] SIGINT  x1 test(s), e.g. test_3",
        "  tests/a/test_x.py  [env] AVA_X  x3 test(s), e.g. test_1",
        "  tests/z/test_z.py  [cwd] /a  x1 test(s), e.g. test_4",
    ]
    assert "### Leak guard" in summary.read_text()
    written = json.loads(report.read_text())
    assert written["complete"] is False
    assert sorted(x["shard"] for x in written["leaks"]) == ["1", "1", "1", "1", "2"]


def test_the_annotation_is_capped_and_the_artifact_keeps_everything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    counts = tmp_path / "counts"
    cases = [
        _guarded(f"tests/a/test_{i:03d}.py", "test_1", ("leak_guard", f"env: KEY_{i} was added"))
        for i in range(100)
    ]
    _leaks_in(counts, "1", *cases)
    report = tmp_path / "leaks.json"
    argv = ["total", "--dir", str(counts), "--expected", "1", "--leak-report", str(report)]
    shard_counts.main(argv)
    lines = _annotation(capsys.readouterr().out)
    assert len(lines) == shard_counts.ANNOTATION_LINES == 40
    assert lines[-1] == "  (+63 more: artifact test-leak-report)"  # 102 lines, 39 shown
    assert len(json.loads(report.read_text())["leaks"]) == 100


def test_a_guard_fault_is_named_in_the_annotation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    counts = tmp_path / "counts"
    fault = ("leak_guard_fault", "snapshot: AttributeError: _gone")
    _leaks_in(counts, "1", _guarded("tests/a/test_x.py", "test_1", fault))
    _leaks_in(counts, "2", _guarded("tests/a/test_x.py", "test_2", fault))
    shard_counts.main(["total", "--dir", str(counts), "--expected", "1 2"])
    lines = _annotation(capsys.readouterr().out)
    assert "0 leak finding(s) in 0 test(s)" in lines[0]
    assert "2 guard fault(s)" in lines[0]
    assert lines[2] == "  GUARD FAULT [snapshot] in 2 test report(s): AttributeError: _gone"


def test_the_annotation_data_is_escaped_onto_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    counts = tmp_path / "counts"
    _leaks_in(counts, "1", _guarded("tests/a/test_x.py", "test_50%[a-b]", ("leak_guard", "env: K")))
    shard_counts.main(["total", "--dir", str(counts), "--expected", "1"])
    command = next(x for x in capsys.readouterr().out.splitlines() if x.startswith("::warning"))
    assert "%0A" in command
    assert "e.g. test_50%25[a-b]" in command


def test_a_fault_in_the_leak_report_is_one_annotation_and_never_the_jobs_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Whatever the count files hold, the total (and the baseline it writes first) succeeds."""
    counts = tmp_path / "counts"
    _counts(counts, "1", {"tests/base": 5})
    broken = json.loads((counts / "shard-counts-1.json").read_text())
    broken["leaks"] = [{"detail": "a leak without a test or a kind"}]
    (counts / "shard-counts-1.json").write_text(json.dumps(broken), encoding="utf-8")
    out = tmp_path / "out.json"
    argv = ["total", "--dir", str(counts), "--expected", "1", "--out", str(out)]
    assert shard_counts.main(argv) == 0
    log = capsys.readouterr().out
    assert "::warning title=leak guard (report fault)::KeyError" in log
    assert json.loads(out.read_text())["tests"] == 5


def test_the_total_job_reports_and_uploads_the_leak_list_without_being_able_to_fail() -> None:
    steps = _steps("backend-test-counts")
    total = steps[_index(steps, "Sum the executed test counts")]
    assert "--leak-report test-leak-report.json" in total["run"]
    upload = steps[_index(steps, "Upload the leak report")]
    assert upload["continue-on-error"] is True
    assert upload["if"] == "${{ !cancelled() }}"
    assert upload["with"]["name"] == "test-leak-report"
    assert upload["with"]["path"] == "test-leak-report.json"
    assert _index(steps, "Sum the executed test counts") < _index(steps, "Upload the leak report")
