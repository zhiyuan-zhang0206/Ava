"""CI artifact-publishing contracts after the 2026-09-22 false red.

Artifact-service 403/500 finalization failures must not turn green test jobs red.
Coverage retries once, then records absent inputs for the unchanged coverage gate.
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
    return cast("dict[str, Any]", matches[0])


def test_every_artifact_upload_is_non_blocking() -> None:
    """Artifact transport cannot red a required job after its tests pass."""
    for job_name, job in _workflow_jobs().items():
        for step in cast("list[dict[str, Any]]", job["steps"]):
            uses = step.get("uses")
            if isinstance(uses, str) and "upload-artifact" in uses:
                step_name = step.get("name", "unnamed step")
                assert step.get("continue-on-error") is True, (
                    f"{job_name} / {step_name} must tolerate artifact-service failures"
                )


def test_coverage_uploads_retry_once_and_expose_the_outcome() -> None:
    """Coverage uploads retry a transient fault before the gate records a gap."""
    for job_name in ("backend-shard", "backend-serial"):
        job = _workflow_jobs()[job_name]
        upload = _step(job, "Upload coverage data")
        assert upload["id"] == "coverage-upload"
        assert upload["continue-on-error"] is True

        retry = _step(job, "Retry coverage data upload")
        assert retry["if"] == "${{ steps.coverage-upload.outcome == 'failure' }}"
        assert retry["continue-on-error"] is True
        retry_with = retry["with"]
        assert isinstance(retry_with, dict)
        assert retry_with["overwrite"] is True

        note = _step(job, "Note coverage upload outcome")
        assert note["if"] == "${{ always() && steps.coverage-upload.outcome == 'failure' }}"
        assert "::warning" in note["run"]


def test_coverage_gate_records_the_groups_that_arrived() -> None:
    """The gate scores received coverage while making dropped shard data visible."""
    jobs = _workflow_jobs()
    shard_groups = cast("list[int]", jobs["backend-shard"]["strategy"]["matrix"]["group"])
    expected_groups = " ".join([*(str(group) for group in shard_groups), "serial"])

    backend_env = jobs["backend"]["env"]
    assert isinstance(backend_env, dict)
    assert backend_env["COVERAGE_DATA_GROUPS"] == expected_groups

    combine = _step(jobs["backend"], "Combine coverage + gate")
    assert "$COVERAGE_DATA_GROUPS" in combine["run"]
    assert "::warning title=coverage data incomplete" in combine["run"]
