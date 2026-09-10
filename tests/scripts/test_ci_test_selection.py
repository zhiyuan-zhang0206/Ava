"""CI wiring contracts for backend test selection (enforce + shadow fallback).

Enforce (the default) replaces the 16-shard fan-out with the selected subset on
a SELECTED pull request; shadow keeps the fan-out as the only gate and runs the
subset informationally. The one revert switch is the workflow-level
TEST_SELECTION_MODE value — these tests pin every routing expression that
depends on it, so an edit that decouples the switch from a gate fails here
instead of silently weakening CI.
"""

from __future__ import annotations

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


def test_enforce_is_the_default_and_test_select_republishes_the_mode() -> None:
    """The revert switch is one value; job-level routing cannot read env, so
    test-select republishes it as an output before anything can fail."""
    document = _workflow()
    assert document["env"]["TEST_SELECTION_MODE"] == "enforce"

    selector = _workflow_jobs()["test-select"]
    # A selector failure keeps the job non-gating: its outputs stay empty and
    # the routing expressions below fall through to the full fan-out.
    assert selector["continue-on-error"] is True
    assert selector["outputs"]["mode"] == "${{ steps.selector.outputs.mode }}"
    selector_step = _step(selector, "Select direct-import test subset")
    assert 'echo "mode=$TEST_SELECTION_MODE" >> "$GITHUB_OUTPUT"' in selector_step["run"]


def test_shards_are_skipped_only_on_the_enforced_subset_path() -> None:
    """Everything except enforce+SELECTED runs the full fan-out: shadow, FULL,
    SKIP, and a selector failure (empty decision) all keep the gate."""
    shard = _workflow_jobs()["backend-shard"]
    assert shard["needs"] == ["classify", "test-select"]
    condition = shard["if"]
    # always(): a skipped test-select (push-to-main) must not propagate a skip
    # down the needs chain into skipped shards.
    assert "always()" in condition
    assert "needs.test-select.outputs.mode != 'enforce'" in condition
    assert "needs.test-select.outputs.decision != 'SELECTED'" in condition


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
    # The Trunk quarantine gate is the decider, exactly as in a shard.
    assert run_step["continue-on-error"] is True
    assert '-m "not flaky" -n 4' in run_step["run"]
    assert "--junit-xml=tmp/junit-backend-selected.xml" in run_step["run"]


def test_subset_and_shard_quarantine_gates_have_the_same_shape() -> None:
    """The subset must not red on quarantined flaky failures any more than a
    shard does — same uploader, same summary step; subset gate enforce-only."""
    jobs = _workflow_jobs()
    subset_gate = _step(jobs["backend-selected"], "Trunk quarantine gate (upload test results)")
    shard_gate = _step(jobs["backend-shard"], "Trunk quarantine gate (upload test results)")
    assert subset_gate["uses"] == shard_gate["uses"]
    assert subset_gate["with"]["junit-paths"] == "tmp/junit-backend-selected.xml"
    assert shard_gate["with"]["junit-paths"] == "tmp/junit-backend-shard-${{ matrix.group }}-*.xml"
    assert "needs.test-select.outputs.mode == 'enforce'" in subset_gate["if"]
    assert "TRUNK_ORG_URL_SLUG" in subset_gate["if"]
    _step(jobs["backend-selected"], "Show unquarantined failure summary")
    _step(jobs["backend-shard"], "Show unquarantined failure summary")


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
    verify = _step(aggregator, "Verify backend job results")["run"]
    assert '"$TEST_SELECTION_MODE" = "enforce"' in verify
    assert '"$DECISION" = "SELECTED"' in verify
    assert 'check backend-selected "${{ needs.backend-selected.result }}"' in verify
    assert 'check backend-shard "${{ needs.backend-shard.result }}"' in verify


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
