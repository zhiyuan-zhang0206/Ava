"""CI wiring contracts for informational backend test-selection shadowing."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_SHADOW_JOBS = (
    "test-select",
    "backend-selected-shadow",
    "test-selection-shadow-report",
)


def _workflow_jobs() -> dict[str, Any]:
    """Load CI jobs while keeping the YAML parser boundary explicit."""
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    return cast("dict[str, Any]", jobs)


def test_shadow_jobs_are_non_gating_and_compare_matching_pytest_populations() -> None:
    """Shadow setup failures and flaky/static gates must not create false greens."""
    jobs = _workflow_jobs()

    assert all(jobs[job]["continue-on-error"] is True for job in _SHADOW_JOBS)
    assert jobs["backend"]["needs"] == [
        "classify",
        "backend-static",
        "backend-structure",
        "backend-shard",
        "backend-serial",
        "backend-pgvector-smoke",
    ]
    assert jobs["test-selection-shadow-report"]["needs"] == [
        "classify",
        "backend",
        "backend-shard",
        "backend-selected-shadow",
        "test-select",
    ]

    subset_run = jobs["backend-selected-shadow"]["steps"][4]["run"]
    assert '-m "not flaky" -n 4' in subset_run
    report_step = jobs["test-selection-shadow-report"]["steps"][0]
    assert report_step["env"] == {
        "FULL_BACKEND_RESULT": "${{ needs.backend.result }}",
        "FULL_PYTEST_RESULT": "${{ needs.backend-shard.result }}",
        "SUBSET_STATUS": "${{ needs.backend-selected-shadow.outputs.subset_status }}",
        "DECISION": "${{ needs.test-select.outputs.decision }}",
        "REASON": "${{ needs.test-select.outputs.reason }}",
        "EST_SECONDS": "${{ needs.test-select.outputs.est_seconds }}",
        "FULL_EST_SECONDS": "${{ needs.test-select.outputs.full_est_seconds }}",
        "PR_NUMBER": "${{ github.event.pull_request.number }}",
        "HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
    }
    report_run = report_step["run"]
    assert '"$FULL_PYTEST_RESULT" = "failure"' in report_run
