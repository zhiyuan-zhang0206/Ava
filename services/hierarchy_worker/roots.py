"""Composition roots of the hierarchy worker: the only module of the package that reads `settings`.

Two processes run this package: the schedule host (`schedules/hierarchy-worker-schedule.py`,
which calls `prepare()` once and `run_tick()` per tick) and the build child
(`services.hierarchy_worker.job`). Both build their configuration here.
"""

from __future__ import annotations

import os

from base.config import settings
from base.db import Database
from base.log import init_gateway_process, logger
from services.hierarchy_worker.config import HierarchyWorkerConfig


def hierarchy_worker_config() -> HierarchyWorkerConfig:
    return HierarchyWorkerConfig(
        hierarchy_model=settings.lm.hierarchy_model,
        hierarchy_job_budget_seconds=settings.daemon.hierarchy_job_budget_seconds,
        hierarchy_job_deadline_seconds=settings.daemon.hierarchy_job_deadline_seconds,
        hierarchy_child_kill_grace_seconds=settings.daemon.hierarchy_child_kill_grace_seconds,
        hierarchy_stale_grace_seconds=settings.daemon.hierarchy_stale_grace_seconds,
        hierarchy_retry_backoff_seconds=settings.daemon.hierarchy_retry_backoff_seconds,
        hierarchy_retry_backoff_cap_seconds=settings.daemon.hierarchy_retry_backoff_cap_seconds,
        hierarchy_generation_concurrency=settings.daemon.hierarchy_generation_concurrency,
        hierarchy_tail_seal_enabled=settings.daemon.hierarchy_tail_seal_enabled,
        hierarchy_tail_idle_minutes=settings.daemon.hierarchy_tail_idle_minutes,
        hierarchy_tail_min_interval_minutes=settings.daemon.hierarchy_tail_min_interval_minutes,
        hierarchy_tail_max_per_tick=settings.daemon.hierarchy_tail_max_per_tick,
        hierarchy_worker_enabled=settings.daemon.hierarchy_worker_enabled,
        hierarchy_worker_agents=settings.daemon.hierarchy_worker_agents,
        hierarchy_fallback_scan_seconds=settings.daemon.hierarchy_fallback_scan_seconds,
        hierarchy_regen_alert_nodes_per_job=settings.daemon.hierarchy_regen_alert_nodes_per_job,
        hierarchy_regen_halt_nodes_per_job=settings.daemon.hierarchy_regen_halt_nodes_per_job,
        hierarchy_regen_daily_budget_nodes=settings.daemon.hierarchy_regen_daily_budget_nodes,
        hierarchy_regen_min_reuse_ratio=settings.daemon.hierarchy_regen_min_reuse_ratio,
    )


def hierarchy_worker_db() -> Database:
    """The handle on the cluster database, built where the worker's process starts."""
    return Database.from_settings()


def tick() -> None:
    """One schedule tick: the runner's drain with this process's configuration and database."""
    from services.hierarchy_worker.runner import run_tick

    run_tick(hierarchy_worker_config(), hierarchy_worker_db())


def prepare() -> None:
    """One-time host start: open the process sinks, verify the schema, announce.

    Called by the schedule host before its first tick. The process-boot seam
    runs first — the schedule runner is otherwise sink-less, so the start /
    scan / claim lines and a drifted-schema crash would all be dropped
    records — then a drifted schema raises, so the manager's crash path
    (backoff + breaker + last_error) exposes it instead of every tick failing
    on its own.
    """
    from base.deploy.schema.migrations import assert_schema_current

    init_gateway_process(name="schedule-hierarchy-worker")
    assert_schema_current(settings.data_plane.db_url)
    logger.info("hierarchy worker started (pid {pid})", pid=os.getpid())
