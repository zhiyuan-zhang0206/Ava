"""The hierarchy-worker slice for tests: the root's builder over the live settings, with overrides.

The builder is called at each use, so a test that sets a setting before the call still reaches
the code under test through the slice.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import psycopg

from base.db import Database
from services.hierarchy_worker import execute as execute_module
from services.hierarchy_worker import roots, runner
from services.hierarchy_worker import scan as scan_module
from services.hierarchy_worker.config import HierarchyWorkerConfig
from services.hierarchy_worker.scan import ScanOutcome


def hierarchy_config(**overrides: Any) -> HierarchyWorkerConfig:
    return replace(roots.hierarchy_worker_config(), **overrides)


def scan(conn: psycopg.Connection) -> ScanOutcome:
    """`scan.scan` with the live settings' slice."""
    return scan_module.scan(conn, hierarchy_config())


def execute_job(job_id: int) -> int:
    """`execute.execute_job` with the live settings' slice and the test database."""
    return execute_module.execute_job(job_id, hierarchy_config(), hierarchy_db())


def hierarchy_db() -> Database:
    """The handle on the test database (the settings the suite's conftest points at the throwaway cluster)."""
    return Database.from_settings()


def run_child(job: runner.ClaimedJob) -> None:
    """`runner.run_child` with the live settings' slice and the test database."""
    runner.run_child(job, hierarchy_config(), hierarchy_db())
