"""Configuration slice of the hierarchy worker.

Fields keep their flat registry names. `services/derived/hierarchy_worker/roots.py` builds the slice
(`hierarchy_worker_config()`, the composition root shared by the schedule tick and the build
child) and hands it to the scan, the runner and the job execution.
"""

from __future__ import annotations

from dataclasses import dataclass

from base.config.domains.daemon.hierarchy_worker_fields import parse_hierarchy_worker_agents


@dataclass(frozen=True)
class HierarchyWorkerConfig:
    hierarchy_model: str
    hierarchy_job_budget_seconds: float
    hierarchy_job_deadline_seconds: float
    hierarchy_child_kill_grace_seconds: float
    hierarchy_stale_grace_seconds: float
    hierarchy_retry_backoff_seconds: float
    hierarchy_retry_backoff_cap_seconds: float
    hierarchy_generation_concurrency: int
    hierarchy_tail_seal_enabled: bool
    hierarchy_tail_idle_minutes: float
    hierarchy_tail_min_interval_minutes: float
    hierarchy_tail_max_per_tick: int
    hierarchy_worker_enabled: bool
    hierarchy_worker_agents: str
    hierarchy_fallback_scan_seconds: float
    hierarchy_regen_alert_nodes_per_job: int
    hierarchy_regen_halt_nodes_per_job: int
    hierarchy_regen_daily_budget_nodes: int
    hierarchy_first_build_daily_budget_nodes: int
    hierarchy_regen_min_reuse_ratio: float

    def served_agents(self) -> frozenset[int]:
        """The rollout allowlist as ids; empty = every agent."""
        return parse_hierarchy_worker_agents(self.hierarchy_worker_agents)
