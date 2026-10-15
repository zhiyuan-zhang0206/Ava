from __future__ import annotations

import re
from pathlib import Path

import yaml

from scripts.ci import refresh_test_durations as refresh

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _workflow_matrix_groups(workflow_name: str, job_name: str) -> list[int]:
    path = _REPO_ROOT / ".github" / "workflows" / workflow_name
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return document["jobs"][job_name]["strategy"]["matrix"]["group"]


def test_shard_counts_track_both_workflow_matrices() -> None:
    ci_backend = _workflow_matrix_groups("ci.yml", "backend-shard")
    nightly_backend = _workflow_matrix_groups("refresh-test-durations.yml", "measure-backend")
    ci_e2e = _workflow_matrix_groups("ci.yml", "e2e-shard")
    nightly_e2e = _workflow_matrix_groups("refresh-test-durations.yml", "measure-e2e")

    assert refresh.BACKEND_SHARDS == len(ci_backend) == len(nightly_backend)
    assert refresh.E2E_SHARDS == len(ci_e2e) == len(nightly_e2e)


def test_coverage_args_track_the_ci_backend_shard() -> None:
    document = yaml.safe_load((_REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    steps = document["jobs"]["backend-shard"]["steps"]
    (command,) = (step["run"] for step in steps if step.get("name") == "Run pytest shard")
    attempts = [
        sorted(re.findall(r"--cov=(\w+)", attempt))
        for attempt in command.split("uv run pytest")[1:]
    ]
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert (
        sorted(arg.removeprefix("--cov=") for arg in refresh.BACKEND_COVERAGE_ARGS) == attempts[0]
    )
