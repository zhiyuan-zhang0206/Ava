"""Native CI test failures and invalid or missing execution evidence are always red."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from scripts.ci import shard_counts

_REPO_ROOT = Path(__file__).resolve().parents[2]
_JOBS = cast(
    "dict[str, Any]", yaml.safe_load((_REPO_ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
)
_ACTION = yaml.safe_load((_REPO_ROOT / ".github/actions/require-test-gate/action.yml").read_text())
_SHAPES = {
    "backend-shard": [("Run pytest shard", "pytest-shard")],
    "backend-selected": [("Run selected pytest subset", "run-subset")],
    "backend-serial": [("Run flaky pytest bucket serially", "pytest-serial")],
    "frontend": [
        ("Unit tests (vitest run)", "vitest"),
        ("Flaky unit tests (vitest run, serial)", "vitest-flaky"),
    ],
    "e2e-shard": [("Run e2e tests", "pytest-e2e")],
    "e2e-hosted": [("Run hosted e2e tests", "pytest-hosted")],
    "e2e-env-guard": [
        ("Run env-guard warmup + home-isolation in one serial process", "pytest-env-guard")
    ],
}


@pytest.mark.parametrize("job", _SHAPES)
def test_native_tests_and_evidence_have_no_secret_or_quarantine_bypass(job: str) -> None:
    steps = _JOBS[job]["steps"]
    for name, identity in _SHAPES[job]:
        native = next(step for step in steps if step.get("name") == name)
        assert native.get("continue-on-error") is not True
        guards = [
            step
            for step in steps
            if step.get("uses") == "./.github/actions/require-test-gate"
            and step["with"]["test-outcome"] == f"${{{{ steps.{identity}.outcome }}}}"
        ]
        assert len(guards) == 1
        guard = guards[0]
        assert guard["if"] == "${{ !cancelled() }}"
        assert "continue-on-error" not in guard
        assert steps.index(native) < steps.index(guard)
    assert "TRUNK" not in json.dumps(_JOBS[job])
    assert not any("trunk-io/analytics-uploader" in step.get("uses", "") for step in steps)


def test_optional_empty_buckets_and_final_attempt_are_explicit() -> None:
    for job, identity in [("backend-serial", "pytest-serial"), ("frontend", "vitest-flaky")]:
        guards = [
            step
            for step in _JOBS[job]["steps"]
            if step.get("uses") == "./.github/actions/require-test-gate"
        ]
        guard = next(step for step in guards if identity in step["with"]["test-outcome"])
        assert guard["with"]["min-tests"] == "0"
    guard = next(
        step
        for step in _JOBS["backend-shard"]["steps"]
        if step.get("uses") == "./.github/actions/require-test-gate"
    )
    assert guard["with"]["latest-report"] == "true"
    assert "-a*.xml" in guard["with"]["junit-patterns"]


def _run_gate(
    tmp_path: Path,
    *,
    outcome: str = "success",
    minimum: int = 1,
    patterns: list[str] | None = None,
    latest: bool = False,
) -> subprocess.CompletedProcess[str]:
    script = _ACTION["runs"]["steps"][0]["run"]
    env = {
        **os.environ,
        "TEST_OUTCOME": outcome,
        "JUNIT_PATTERNS": json.dumps(patterns or [str(tmp_path / "report.xml")]),
        "MIN_TESTS": str(minimum),
        "LATEST_REPORT": str(latest).lower(),
    }
    return subprocess.run(  # noqa: S603 - execute the repository action with synthetic reports
        ["bash", "-e", "-c", script], env=env, capture_output=True, text=True, check=False
    )


def _report(tmp_path: Path, cases: str, *, attributes: str = "") -> None:
    (tmp_path / "report.xml").write_text(
        f"<testsuites><testsuite {attributes}>{cases}</testsuite></testsuites>"
    )


@pytest.mark.parametrize("outcome", ["failure", "cancelled", "skipped"])
def test_native_failure_cannot_be_overruled_by_a_passing_report(
    tmp_path: Path, outcome: str
) -> None:
    _report(tmp_path, '<testcase name="passes"/>')
    result = _run_gate(tmp_path, outcome=outcome)
    assert result.returncode == 1
    assert "native test step ended " + outcome in result.stdout


@pytest.mark.parametrize("kind", ["failure", "error"])
def test_recorded_failure_cannot_be_overruled_by_a_success_exit(tmp_path: Path, kind: str) -> None:
    _report(tmp_path, f'<testcase classname="Example" name="broken"><{kind}/></testcase>')
    result = _run_gate(tmp_path)
    assert result.returncode == 1
    assert "FAILED Example::broken" in result.stdout


@pytest.mark.parametrize(
    "contents",
    [
        None,
        "<broken",
        "<unrelated/>",
        "<testsuites/>",
        '<testsuite><testcase name="skip"><skipped/></testcase></testsuite>',
    ],
)
def test_missing_malformed_or_unexecuted_report_is_red(
    tmp_path: Path, contents: str | None
) -> None:
    if contents is not None:
        (tmp_path / "report.xml").write_text(contents)
    assert _run_gate(tmp_path).returncode == 1


def test_every_required_report_must_exist(tmp_path: Path) -> None:
    _report(tmp_path, '<testcase name="passes"/>')
    assert (
        _run_gate(
            tmp_path, patterns=[str(tmp_path / "report.xml"), str(tmp_path / "absent.xml")]
        ).returncode
        == 1
    )


def test_explicitly_empty_bucket_requires_a_valid_successful_report(tmp_path: Path) -> None:
    assert _run_gate(tmp_path, minimum=0).returncode == 1
    _report(tmp_path, "")
    assert _run_gate(tmp_path, minimum=0).returncode == 0
    assert _run_gate(tmp_path, minimum=0, outcome="failure").returncode == 1
    _report(tmp_path, "", attributes='errors="1"')
    assert _run_gate(tmp_path, minimum=0).returncode == 1


def test_final_attempt_is_validated_without_erasing_first_failure(tmp_path: Path) -> None:
    first = tmp_path / "report-a1.xml"
    final = tmp_path / "report-a2.xml"
    first.write_text('<testsuite><testcase name="broken"><failure/></testcase></testsuite>')
    final.write_text('<testsuite><testcase name="passes"/></testsuite>')
    pattern = [str(tmp_path / "report-a*.xml")]
    assert _run_gate(tmp_path, patterns=pattern, latest=True).returncode == 0
    assert _run_gate(tmp_path, patterns=pattern).returncode == 1
    assert "broken" in first.read_text()


def _pytest(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    return subprocess.run(  # noqa: S603 - our own interpreter, synthetic tree
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _count(report: Path, out: Path, min_tests: int) -> subprocess.CompletedProcess[str]:
    script = _REPO_ROOT / "scripts/ci/shard_counts.py"
    return subprocess.run(  # noqa: S603 - the repository's own script
        [
            sys.executable,
            str(script),
            "shard",
            "--group",
            "1",
            "--junit",
            str(report),
            "--out",
            str(out),
            "--min-tests",
            str(min_tests),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_pytest_that_dies_before_writing_its_report_is_red(tmp_path: Path) -> None:
    """The reproduction: a `pytest_plugins` module that fails to import ends pytest in the
    configuration phase, before it can write a JUnit report."""
    (tmp_path / "broken_plugin.py").write_text("raise ImportError('boom')\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text('pytest_plugins = ["broken_plugin"]\n', encoding="utf-8")
    (tmp_path / "test_a.py").write_text("def test_a() -> None:\n    pass\n", encoding="utf-8")
    report = tmp_path / "tmp" / "junit-backend-shard-1-a1.xml"
    result = _pytest(tmp_path, f"--junit-xml={report}", "-o", "junit_family=xunit1")
    assert result.returncode != 0
    assert not report.exists()  # the native process failed before producing evidence
    counted = _count(tmp_path / "tmp" / "junit-backend-shard-1-a*.xml", tmp_path / "c.json", 1)
    assert counted.returncode != 0
    assert "no JUnit report matches" in counted.stderr


def test_a_pytest_that_ran_no_test_is_red(tmp_path: Path) -> None:
    """The report is there and empty (`testpaths` matched nothing, a marker deselected it all)."""
    (tmp_path / "test_a.py").write_text("def test_a() -> None:\n    pass\n", encoding="utf-8")
    report = tmp_path / "tmp" / "junit-backend-shard-1-a1.xml"
    result = _pytest(
        tmp_path,
        "-m",
        "nothing_has_this_marker",
        f"--junit-xml={report}",
        "-o",
        "junit_family=xunit1",
    )
    assert result.returncode == 5, result.stdout
    assert shard_counts.count_junit(report)["tests"] == 0
    counted = _count(report, tmp_path / "c.json", 1)
    assert counted.returncode != 0
    assert "executed 0 tests" in counted.stderr
    assert _count(report, tmp_path / "c.json", 0).returncode == 0  # the flaky bucket's rule
