"""CI wiring contracts for backend test selection (enforce + shadow fallback).

Enforce (the default) replaces the 16-shard fan-out with the selected subset on
a SELECTED pull request; shadow keeps the fan-out as the only gate and runs the
subset informationally. The one revert switch is the workflow-level
TEST_SELECTION_MODE value — these tests pin every routing expression that
depends on it, so an edit that decouples the switch from a gate fails here
instead of silently weakening CI.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _workflow() -> dict[str, Any]:
    """Load the workflow while keeping the YAML parser boundary explicit."""
    document = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return cast("dict[str, Any]", document)


def _workflow_jobs() -> dict[str, Any]:
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    return cast("dict[str, Any]", jobs)


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [step for step in job["steps"] if step.get("name") == name]
    assert len(matches) == 1, f"expected exactly one step named {name!r}"
    return matches[0]


def _backend_verdict(
    script: str,
    backend: str,
    results: dict[str, str],
    *,
    mode: str = "enforce",
    decision: str = "FULL",
) -> subprocess.CompletedProcess[str]:
    script = script.replace("${{ needs.classify.outputs.backend }}", backend)
    for job, result in results.items():
        script = script.replace("${{ needs." + job + ".result }}", result)
    assert "${{" not in script
    return subprocess.run(  # noqa: S603 - checked-in verifier over closed test-owned results
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        env=os.environ | {"TEST_SELECTION_MODE": mode, "DECISION": decision},
        capture_output=True,
        text=True,
        check=False,
    )


def _assert_backend_verdicts(script: str, dependencies: list[str]) -> None:
    results = dict.fromkeys(["classify", "test-select", *dependencies], "success")
    non_backend = dict.fromkeys(results, "skipped") | {"classify": "success"}
    for outcome in ("success", "failure", "cancelled", "skipped"):
        actual = _backend_verdict(script, "false", non_backend | {"backend-structure": outcome})
        assert actual.returncode == (0 if outcome == "success" else 1), actual.stdout
        assert "backend-static:" not in actual.stdout
        actual = _backend_verdict(
            script, "false", non_backend | {"classify": outcome, "backend-structure": "success"}
        )
        assert actual.returncode == (0 if outcome == "success" else 1), actual.stdout
    for mode, decision, changes, expected in (
        ("enforce", "FULL", {}, 0),
        ("shadow", "SELECTED", {"backend-shard": "failure"}, 1),
        ("enforce", "SELECTED", {"backend-shard": "skipped"}, 0),
        ("enforce", "SELECTED", {"backend-shard": "failure"}, 1),
        (
            "enforce",
            "SELECTED",
            {"backend-shard": "skipped", "backend-selected": "failure"},
            1,
        ),
    ):
        actual = _backend_verdict(script, "true", results | changes, mode=mode, decision=decision)
        assert actual.returncode == expected, actual.stdout
    for job in results:
        for outcome in ("failure", "cancelled", "skipped"):
            actual = _backend_verdict(script, "true", results | {job: outcome})
            # FULL has no enforced subset; every required dependency must succeed.
            assert actual.returncode == (
                0
                if job == "backend-selected" or (job == "test-select" and outcome == "skipped")
                else 1
            ), actual.stdout


def test_enforce_is_the_default_and_test_select_republishes_the_mode() -> None:
    """The revert switch is one value; job-level routing cannot read env, so
    test-select republishes it as an output before anything can fail."""
    document = _workflow()
    assert document["env"]["TEST_SELECTION_MODE"] == "enforce"

    selector = _workflow_jobs()["test-select"]
    # A parser failure retains the full net and fails the required backend gate.
    assert selector.get("continue-on-error", False) is False
    assert selector["outputs"]["mode"] == "${{ steps.selector.outputs.mode }}"
    selector_step = _step(selector, "Select runtime-impact test subset")
    assert 'echo "mode=$TEST_SELECTION_MODE" >> "$GITHUB_OUTPUT"' in selector_step["run"]
    assert '--base-ref "$(cat /tmp/test-selection-base.txt)"' in selector_step["run"]
    changes = _step(selector, "List changed PR paths")["run"]
    assert 'git merge-base "$BASE_SHA" HEAD > /tmp/test-selection-base.txt' in changes
    assert '"$(cat /tmp/test-selection-base.txt)"...HEAD' in changes


def test_shards_are_skipped_only_on_the_enforced_subset_path(tmp_path: Path) -> None:
    """Everything except enforce+SELECTED runs the full fan-out: shadow, FULL,
    SKIP, and a selector failure (empty decision) all keep the gate."""
    shard = _workflow_jobs()["backend-shard"]
    assert shard["needs"] == ["classify", "test-select"]
    condition = shard["if"]
    # A status function admits skipped needs on main, but respects run cancellation.
    assert "!cancelled()" in condition
    assert "needs.test-select.outputs.mode != 'enforce'" in condition
    assert "needs.test-select.outputs.decision != 'SELECTED'" in condition
    run = _step(shard, "Run pytest shard")["run"]
    assert '"--collect-only", "-qq"' in run
    assert '"--file-shard-plan=tmp/file-shards/plan.json"' in run
    assert run.count("--file-shard-check=tmp/file-shards/plan.json") == 2
    assert run.count("--file-shard-group=${{ matrix.group }}") == 2
    assert run.count("--file-shard-execute") == 2
    assert "--splits" not in run
    _assert_shard_failure_verdicts(tmp_path, run)


def _assert_shard_failure_verdicts(root: Path, script: str) -> None:
    """Exercise the actual shell boundary without native services or real timers."""
    binary = root / "bin"
    binary.mkdir()
    timeout = binary / "timeout"
    timeout.write_text('#!/bin/sh\nshift 2\nexec "$@"\n')
    timeout.chmod(0o755)
    uv = binary / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "path = pathlib.Path('calls.json')\n"
        "calls = json.loads(path.read_text()) if path.exists() else []\n"
        "calls.append(sys.argv[1:])\n"
        "path.write_text(json.dumps(calls))\n"
        "if sys.argv[2] == 'python':\n"
        "    sys.exit(int(os.environ['PLAN_STATUS']))\n"
        "duration = pathlib.Path('tmp/durations/backend-' + os.environ['GROUP'] + '.json')\n"
        "assert duration.read_text() == '{}'\n"
        "duration.write_text('{\"measured\": 1}')\n"
        "attempt = sum(call[1] == 'pytest' for call in calls)\n"
        "sys.exit(int(os.environ['FIRST_STATUS' if attempt == 1 else 'FINAL_STATUS']))\n"
    )
    uv.chmod(0o755)
    cases = [
        (23, 0, 0, 1, 23, 0),
        (0, 2, 0, 1, 2, 1),
        (0, 3, 0, 1, 3, 1),
        (0, 4, 0, 1, 4, 1),
        (0, 1, 0, 1, 0, 2),
        (0, 1, 19, 1, 19, 2),
        (0, 1, 0, 8, 1, 1),
        (0, 0, 0, 1, 0, 1),
    ]
    for index, (plan_status, first, final, group, expected, attempts) in enumerate(cases):
        directory = root / str(index)
        directory.mkdir()
        (directory / ".test_durations").write_text("{}")
        env = {
            **os.environ,
            "PATH": str(binary) + os.pathsep + os.environ["PATH"],
            "PLAN_STATUS": str(plan_status),
            "FIRST_STATUS": str(first),
            "FINAL_STATUS": str(final),
            "GROUP": str(group),
            "RECORD_DURATIONS": "true",
            "COVERAGE_FILE": "coverage-data",
            "GITHUB_OUTPUT": str(directory / "outputs"),
        }
        result = subprocess.run(  # noqa: S603 -- repository shell with test-owned commands
            [
                "bash",
                "--noprofile",
                "--norc",
                "-eo",
                "pipefail",
                "-c",
                script.replace("${{ matrix.group }}", str(group)),
            ],
            cwd=directory,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == expected, result.stdout + result.stderr
        calls = cast("list[list[str]]", json.loads((directory / "calls.json").read_text()))
        assert len(calls) == 1 + attempts
        assert sum(call[1] == "python" for call in calls) == 1


def test_subset_job_gates_only_in_enforce() -> None:
    subset = _workflow_jobs()["backend-selected"]
    assert subset["needs"] == ["classify", "test-select"]
    assert subset["continue-on-error"] == "${{ needs.test-select.outputs.mode != 'enforce' }}"
    assert subset["if"] == (
        "${{ github.event_name == 'pull_request' && needs.classify.outputs.backend == 'true' "
        "&& needs.test-select.outputs.decision == 'SELECTED' }}"
    )
    assert subset["outputs"]["subset_status"] == "${{ steps.run-subset.outputs.subset_status }}"
    run_step = _step(subset, "Run selected pytest subset")
    assert "continue-on-error" not in run_step
    assert 'exit "$status"' in run_step["run"]
    assert '-m "not flaky" -n 4' in run_step["run"]
    assert "--junit-xml=tmp/junit-backend-selected.xml" in run_step["run"]


def test_zero_execution_never_reads_green() -> None:
    guard = _step(_workflow_jobs()["backend-selected"], "Validate native test results")
    assert guard["if"] == "${{ !cancelled() }}"
    assert guard["uses"] == "./.github/actions/require-test-gate"
    assert guard["with"]["junit-patterns"] == '["tmp/junit-backend-selected.xml"]'


def test_subset_and_shard_native_gates_have_the_same_shape() -> None:
    jobs = _workflow_jobs()
    subset_gate = _step(jobs["backend-selected"], "Validate native test results")
    shard_gate = _step(jobs["backend-shard"], "Validate native test results")
    assert subset_gate["uses"] == shard_gate["uses"] == "./.github/actions/require-test-gate"
    assert subset_gate["if"] == shard_gate["if"] == "${{ !cancelled() }}"
    assert "TRUNK" not in str(subset_gate) + str(shard_gate)
    assert shard_gate["with"]["latest-report"] == "true"


def test_aggregator_requires_whichever_pytest_path_ran() -> None:
    """One required check fans in both paths: the selected subset under
    enforce+SELECTED, the full fan-out otherwise."""
    aggregator = _workflow_jobs()["backend"]
    assert aggregator["needs"] == [
        "classify",
        "test-select",
        "backend-static",
        "backend-structure",
        "backend-shard",
        "backend-selected",
        "backend-serial",
        "backend-pgvector-smoke",
        "helper-signing-smoke",
    ]
    assert aggregator["if"] == (
        "${{ !cancelled() && (needs.classify.result == 'failure' || "
        "(needs.classify.result == 'success' && (needs.classify.outputs.backend == 'true' || "
        "needs.backend-structure.result != 'success'))) }}"
    )
    verify = _step(aggregator, "Verify backend job results")["run"]
    assert '"$TEST_SELECTION_MODE" = "enforce"' in verify
    assert '"$DECISION" = "SELECTED"' in verify
    assert 'check backend-selected "${{ needs.backend-selected.result }}"' in verify
    assert 'check backend-shard "${{ needs.backend-shard.result }}"' in verify
    assert verify.index('check backend-structure "${{ needs.backend-structure.result }}"') < (
        verify.index('if [ "$TEST_SELECTION_MODE" = "enforce" ]')
    )
    _assert_backend_verdicts(verify, aggregator["needs"][2:])


def test_backend_admission_preserves_draft_skip_and_propagates_classify_failure() -> None:
    condition = _workflow_jobs()["backend"]["if"].removeprefix("${{").removesuffix("}}")
    for classify, backend, structure, cancelled, expected in (
        ("skipped", "", "skipped", False, False),  # draft PR
        ("failure", "", "skipped", False, True),  # runtime analysis failure
        ("success", "true", "success", False, True),
        ("success", "false", "success", False, False),
        ("success", "false", "failure", False, True),
        ("failure", "", "skipped", True, False),
    ):
        closed = condition.replace("!cancelled()", "0 == 1" if cancelled else "1 == 1")
        for key, value in (
            ("needs.classify.result", classify),
            ("needs.classify.outputs.backend", backend),
            ("needs.backend-structure.result", structure),
        ):
            closed = closed.replace(key, repr(value))
        assert "needs." not in closed
        result = subprocess.run(  # noqa: S603 - repository predicate with closed test-owned values
            ["bash", "--noprofile", "--norc", "-c", f"[[ {closed} ]]"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode in (0, 1), result.stderr
        assert (result.returncode == 0) is expected, (classify, backend, structure, cancelled)


def test_static_contracts_run_once_outside_the_native_data_plane() -> None:
    jobs = _workflow_jobs()
    static = _step(jobs["backend-static"], "Run static pytest contracts")
    assert static["if"] == "needs.classify.outputs.backend == 'true'"
    assert "--test-environment=static" in static["run"]
    assert "--junit-xml=tmp/junit-backend-static.xml" in static["run"]
    assert "-o junit_family=xunit1" in static["run"]
    assert "continue-on-error" not in static
    for job, name in (
        ("backend-shard", "Run pytest shard"),
        ("backend-selected", "Run selected pytest subset"),
        ("backend-serial", "Run flaky pytest bucket serially"),
    ):
        run = _step(jobs[job], name)["run"]
        if job == "backend-shard":
            planning, run = run.split("\nPY\n", 1)
            assert '"--omit-static-tests"' in planning
            assert '"--collect-only"' in planning
        assert run.count("--omit-static-tests") == run.count("uv run pytest") > 0


def test_coverage_gate_stays_on_the_full_fanout() -> None:
    coverage = _step(_workflow_jobs()["backend"], "Combine coverage + gate")
    assert coverage["if"] == (
        "${{ !(env.TEST_SELECTION_MODE == 'enforce'"
        " && needs.test-select.outputs.decision == 'SELECTED') }}"
    )


def test_shadow_report_stands_down_under_enforce() -> None:
    """The comparison report needs a full pytest result; under enforce a
    SELECTED PR has none, so the report only runs in shadow mode."""
    jobs = _workflow_jobs()
    report = jobs["test-selection-shadow-report"]
    assert "needs.test-select.outputs.mode != 'enforce'" in report["if"]
    assert report["needs"] == [
        "classify",
        "backend",
        "backend-shard",
        "backend-selected",
        "test-select",
    ]
    report_step = report["steps"][0]
    assert report_step["env"] == {
        "FULL_BACKEND_RESULT": "${{ needs.backend.result }}",
        "FULL_PYTEST_RESULT": "${{ needs.backend-shard.result }}",
        "SUBSET_STATUS": "${{ needs.backend-selected.outputs.subset_status }}",
        "DECISION": "${{ needs.test-select.outputs.decision }}",
        "REASON": "${{ needs.test-select.outputs.reason }}",
        "EST_SECONDS": "${{ needs.test-select.outputs.est_seconds }}",
        "FULL_EST_SECONDS": "${{ needs.test-select.outputs.full_est_seconds }}",
        "PR_NUMBER": "${{ github.event.pull_request.number }}",
        "HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
    }
    assert '"$FULL_PYTEST_RESULT" = "failure"' in report_step["run"]


def test_the_retired_shadow_job_name_is_gone() -> None:
    """backend-selected-shadow became backend-selected when the job gained its
    enforce role; a leftover reference would dangle."""
    assert "backend-selected-shadow" not in _WORKFLOW.read_text(encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(  # noqa: S603 -- fixed git commands over test-owned paths
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=CI test",
            "-c",
            "user.email=ci@test.invalid",
            *args,
        ],
        text=True,
    ).strip()


def _changed_repo(repo: Path, paths: tuple[str, ...]) -> str:
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    for path in paths:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("changed\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "changed")
    return base


def test_native_classify_step_preserves_document_and_queue_routing(tmp_path: Path) -> None:
    step = _step(_workflow_jobs()["classify"], "Classify changed paths")
    assert step["env"]["HEAD_REF"] == "${{ github.head_ref }}"
    doc = "scripts/lint/docs/lint.ava.okf.md"
    cases = [
        ("pull_request", "codex/docs", (doc,), ("false", "false")),
        ("pull_request", "codex/mixed", (doc, "scripts/tests/test_x.py"), ("false", "true")),
        (
            "pull_request",
            "codex/frontend-docs",
            ("ui/web/src/docs/page.ava.okf.md",),
            ("false", "false"),
        ),
        (
            "pull_request",
            "codex/test-data",
            ("ui/web/tests/docs/data.ava.okf.md",),
            ("true", "false"),
        ),
        ("pull_request", "trunk-merge/batch-42", (doc,), ("false", "true")),
        ("pull_request", "trunk-temp/batch-42", (doc,), ("false", "true")),
        ("push", "", (doc,), ("true", "true")),
    ]
    for index, (event, head_ref, paths, expected) in enumerate(cases):
        repo = tmp_path / f"repo-{index}"
        base = _changed_repo(repo, paths)
        output = tmp_path / f"outputs-{index}"
        _run_classify(
            step["run"], repo, base, output, event=event, head_ref=head_ref
        ).check_returncode()
        values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        assert (values["frontend"], values["backend"]) == expected, (event, head_ref, paths)


def _run_classify(
    script: str, repo: Path, base: str, output: Path, *, event: str, head_ref: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {
        "EVENT": event,
        "HEAD_REF": head_ref,
        "BASE_SHA": base,
        "GITHUB_OUTPUT": str(output),
        "PYTHONPATH": str(_REPO_ROOT),
        "TMPDIR": str(output.parent),
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
    }
    result = subprocess.run(  # noqa: S603 -- checked-in workflow over test-owned inputs
        ["bash", "-eu", "-c", script],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert not list(output.parent.glob("ava-classify.*")), "classify scratch leaked after exit"
    return result


def test_native_classify_routes_document_resources_and_propagates_analysis_errors(
    tmp_path: Path,
) -> None:
    step = _step(_workflow_jobs()["classify"], "Classify changed paths")
    for index, source in enumerate(
        (
            'from pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\n(ROOT / "docs/runtime.md").read_text()\n',
            "def syntax error\n",
        )
    ):
        repo = tmp_path / f"repo-{index}"
        _changed_repo(repo, ("docs/runtime.md",))
        (repo / "tests").mkdir()
        (repo / "tests/test_document.py").write_text(source)
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "base reader")
        base = _git(repo, "rev-parse", "HEAD")
        (repo / "docs/runtime.md").write_text("updated runtime input\n")
        _git(repo, "commit", "-qam", "document input")
        assert _git(repo, "diff", "--name-only", base, "HEAD") == "docs/runtime.md"
        output = tmp_path / f"outputs-{index}"
        result = _run_classify(
            step["run"], repo, base, output, event="pull_request", head_ref="codex/docs"
        )
        if index == 0:
            result.check_returncode()
            assert dict(line.split("=", 1) for line in output.read_text().splitlines()) == {
                "frontend": "false",
                "backend": "true",
            }
        else:
            assert result.returncode != 0
            assert "SyntaxError" in result.stderr
