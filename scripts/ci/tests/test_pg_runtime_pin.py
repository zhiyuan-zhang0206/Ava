"""CI tests against the Postgres production runs: the vendored zonky tree, not an apt build.

PGDG keeps only its newest 17.x, so no apt pin can match `runtime_binaries._PG_VERSION`; the
backend jobs instead build that tree (`scripts/ci/vendor_pg_runtime.py`) and every pytest
session links it (`tests/fixtures/env_bootstrap.py`). These tests fail when a job installs the
data plane without wiring the vendored tree, or when the wiring drifts from the pin.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOWS = _REPO_ROOT / ".github/workflows"
_SYNC_MARKERS = ("actions/uv-sync", "uv sync --frozen")


def test_every_job_that_installs_the_data_plane_also_vendors_the_postgres_runtime() -> None:
    mismatched = {
        workflow.name: (
            workflow.read_text(encoding="utf-8").count("./.github/actions/install-pg-redis"),
            workflow.read_text(encoding="utf-8").count("./.github/actions/vendor-pg-runtime"),
        )
        for workflow in sorted(_WORKFLOWS.glob("*.yml"))
        if "./.github/actions/install-pg-redis" in workflow.read_text(encoding="utf-8")
    }
    assert mismatched, "no workflow installs the data plane"
    assert {name: pair for name, pair in mismatched.items() if pair[0] != pair[1]} == {}


def test_vendored_runtime_action_is_keyed_on_the_pin_file_and_runs_after_deps() -> None:
    text = (_REPO_ROOT / ".github/actions/vendor-pg-runtime/action.yml").read_text(encoding="utf-8")
    assert "hashFiles('base/cluster/dataplane/runtime_binaries.py')" in text
    assert 'uv run python scripts/ci/vendor_pg_runtime.py "$GITHUB_ENV"' in text
    for workflow in _WORKFLOWS.glob("*.yml"):
        body = workflow.read_text(encoding="utf-8")
        for job in re.split(r"\n  (?=[a-z0-9-]+:\n)", body):
            if "./.github/actions/vendor-pg-runtime" not in job:
                continue
            sync = min((job.find(marker) for marker in _SYNC_MARKERS if marker in job), default=-1)
            assert 0 <= sync < job.index("vendor-pg-runtime"), (
                f"{workflow.name}: vendored runtime built before the Python deps"
            )


def test_the_session_home_links_the_vendored_runtime_the_job_exports() -> None:
    script = (_REPO_ROOT / "scripts/ci/vendor_pg_runtime.py").read_text(encoding="utf-8")
    bootstrap = (_REPO_ROOT / "tests/fixtures/env_bootstrap.py").read_text(encoding="utf-8")
    assert 'f"CI_VENDORED_RUNTIME_ROOT={root}' in script
    assert 'os.environ.get("CI_VENDORED_RUNTIME_ROOT")' in bootstrap
    assert '_TEST_AVA_HOME / "runtime"' in bootstrap


def test_ci_no_longer_installs_an_apt_pgvector() -> None:
    # pgvector rides the vendored tree (the pinned injection); an apt copy would only
    # be a second, unpinned one.
    text = (_REPO_ROOT / ".github/actions/install-pg-redis/action.yml").read_text(encoding="utf-8")
    assert "postgresql-17-pgvector" not in text
