"""The backend jobs' test gate is fail-closed: a run that ran no tests, or has no gate, is red.

Each backend pytest step is `continue-on-error`, so a pytest failure is red only when the
Trunk quarantine gate (`trunk-io/analytics-uploader`) says so. That gate used to fail open
twice:

1. it is skipped when `TRUNK_ORG_URL_SLUG` is empty, and then nothing judged pytest's failures;
2. `allow-missing-junit-files` defaults to true, so a pytest that died before writing its JUnit
   report (a `pytest_plugins` entry that fails to import ends the run in the configuration
   phase) uploaded nothing and passed, with not one test run. So did a pytest that ran zero
   tests (a broken `testpaths`), whose report is present and empty.

These tests pin the wiring in ci.yml, run the composite action's script, and reproduce both
failures with a real pytest.
"""

from __future__ import annotations

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
    "dict[str, Any]",
    yaml.safe_load((_REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))["jobs"],
)
_ACTION = yaml.safe_load(
    (_REPO_ROOT / ".github/actions/require-test-gate/action.yml").read_text(encoding="utf-8")
)
_GATE = "Trunk quarantine gate (upload test results)"
_REQUIRE = "Require the test gate"

# job -> (id of its pytest step, does the job gate only in enforce mode)
_JOB_SHAPES = {
    "backend-shard": ("pytest-shard", False),
    "backend-selected": ("run-subset", True),
    "backend-serial": ("pytest-serial", False),
}


def _steps(job: str) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", _JOBS[job]["steps"])


def _step(job: str, name: str) -> dict[str, Any]:
    matches = [step for step in _steps(job) if step.get("name") == name]
    assert len(matches) == 1, f"{job}: expected exactly one step named {name!r}"
    return matches[0]


def _position(job: str, name: str) -> int:
    return _steps(job).index(_step(job, name))


# ── the wiring ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("job", _JOB_SHAPES)
def test_the_trunk_gate_no_longer_lets_a_missing_report_pass(job: str) -> None:
    gate = _step(job, _GATE)
    assert gate["uses"].startswith("trunk-io/analytics-uploader@")
    assert gate["with"]["allow-missing-junit-files"] is False


@pytest.mark.parametrize("job", _JOB_SHAPES)
def test_an_empty_secret_stops_the_job_instead_of_skipping_the_gate(job: str) -> None:
    pytest_id, enforce_only = _JOB_SHAPES[job]
    require = _step(job, _REQUIRE)
    assert require["uses"] == "./.github/actions/require-test-gate"
    assert require["with"]["pytest-outcome"] == f"${{{{ steps.{pytest_id}.outcome }}}}"
    pytest_step = next(step for step in _steps(job) if step.get("id") == pytest_id)
    assert pytest_step["continue-on-error"] is True  # what makes the gate the only judge
    # It takes over exactly when the gate is skipped, and no other time.
    gate_if = _step(job, _GATE)["if"]
    assert "env.TRUNK_ORG_URL_SLUG != ''" in gate_if
    mode = " && needs.test-select.outputs.mode == 'enforce'" if enforce_only else ""
    assert require["if"] == f"${{{{ !cancelled() && env.TRUNK_ORG_URL_SLUG == ''{mode} }}}}"
    assert gate_if == f"${{{{ !cancelled() && env.TRUNK_ORG_URL_SLUG != ''{mode} }}}}"
    assert _position(job, _REQUIRE) < _position(job, _GATE)


def test_a_backend_shard_that_ran_no_test_is_red() -> None:
    report = _step("backend-shard", "Report executed test counts")
    assert "continue-on-error" not in report
    assert report["if"] == "${{ !cancelled() }}"
    assert "--min-tests 1" in report["run"]
    assert "-a*.xml" in report["run"]  # the last attempt's report, whichever attempt that is


def test_the_flaky_bucket_may_be_empty_but_must_leave_a_report() -> None:
    report = _step("backend-serial", "Report executed test counts")
    assert "continue-on-error" not in report
    assert "--min-tests 0" in report["run"]


def test_the_composite_expects_secrets_only_from_this_repositorys_own_runs() -> None:
    env = _ACTION["runs"]["steps"][0]["env"]["CAN_HAVE_SECRETS"]
    assert "github.repository == 'zhiyuan-zhang0206/Ava'" in env
    assert "github.event.pull_request.head.repo.full_name == github.repository" in env
    assert _ACTION["runs"]["steps"][0]["env"]["PYTEST_OUTCOME"] == "${{ inputs.pytest-outcome }}"


def test_a_dependabot_run_never_counts_as_able_to_have_secrets() -> None:
    # A Dependabot-triggered run is same-repo yet gets no secrets (GitHub withholds
    # them from dependabot[bot]); it must take the no-secrets path and judge pytest.
    env = _ACTION["runs"]["steps"][0]["env"]["CAN_HAVE_SECRETS"]
    assert "&& github.actor != 'dependabot[bot]'" in env


# ── the composite action's script ───────────────────────────────────────────


def _run_gate(can_have_secrets: str, outcome: str) -> subprocess.CompletedProcess[str]:
    script = _ACTION["runs"]["steps"][0]["run"]
    env = {**os.environ, "CAN_HAVE_SECRETS": can_have_secrets, "PYTEST_OUTCOME": outcome}
    return subprocess.run(  # noqa: S603 - the workflow's own script, fixed inputs
        ["bash", "-e", "-c", script], env=env, capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled", "skipped"])
def test_a_run_that_should_have_the_secret_fails_when_it_is_empty(outcome: str) -> None:
    result = _run_gate("true", outcome)
    assert result.returncode == 1
    assert "::error::TRUNK_ORG_URL_SLUG is empty, the test gate cannot run" in result.stdout


@pytest.mark.parametrize("outcome", ["failure", "cancelled", "skipped"])
def test_a_run_without_secrets_fails_on_pytests_own_outcome(outcome: str) -> None:
    result = _run_gate("false", outcome)
    assert result.returncode == 1
    assert f"pytest ended '{outcome}'" in result.stdout


def test_a_run_without_secrets_passes_when_pytest_passed() -> None:
    assert _run_gate("false", "success").returncode == 0


# ── the two fail-open cases, with a real pytest ─────────────────────────────


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
    assert not report.exists()  # nothing for the uploader to see: it passed this run
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
