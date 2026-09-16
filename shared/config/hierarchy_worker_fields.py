"""The hierarchy-worker field block of `DaemonSettings`.

Split out of `shared/config/daemon.py` at birth for the same line-budget
reason the delivery-watchdog block was split (task #3616 x #3621): the daemon
module carries an 800-line hard ceiling. Mixed into `DaemonSettings` — NOT a
config domain: `settings.daemon.hierarchy_*`, every alias/scope/capability
face, and the `.env` contract stay exactly as they would in `daemon.py`. The
budget<deadline cross-check stays on the model in `daemon.py` next to the
delivery-watchdog validators.

These knobs drive the understanding-tree worker (task #3704 P2b) — a
gateway-hosted schedule (`schedules/hierarchy-worker-schedule.py`, one cron
slot a minute) that scans for new compaction boundaries and runs per-agent
builds through `services.hierarchy_worker`. The model it generates with is
`settings.lm.hierarchy_model`.
"""

from __future__ import annotations

from pydantic import Field


class HierarchyWorkerFields:
    """The hierarchy-worker fields, in their `daemon.py` order."""

    hierarchy_job_budget_seconds: float = Field(
        default=2400.0,
        gt=0,
        alias="AVA_HIERARCHY_JOB_BUDGET_SECONDS",
        description=(
            "Per-job generation budget (seconds) — the worker's own stop "
            "point, below the hard deadline. The measured worst full-window "
            "build (93 compact segments: ~75s load + ~889 node generations) "
            "fits inside it; a bigger history is sliced (newest stretches "
            "first) and the continuation job resumes from the hash cache, "
            "never redoing completed nodes."
        ),
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    hierarchy_job_deadline_seconds: float = Field(
        default=3600.0,
        gt=0,
        alias="AVA_HIERARCHY_JOB_DEADLINE_SECONDS",
        description=(
            "Hard ceiling (seconds) on one build child process; the worker "
            "kills it past this. Budget (2400s) + load/write/exit margin: the "
            "deadline exists to bound a wedged child, not to schedule work."
        ),
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    hierarchy_child_kill_grace_seconds: float = Field(
        default=30.0,
        gt=0,
        alias="AVA_HIERARCHY_CHILD_KILL_GRACE_SECONDS",
        description=(
            "SIGTERM grace before a runaway job child is SIGKILLed (seconds). "
            "Bounds how long the runner lingers behind a wedged child at stop "
            "time without cutting off a child mid-write."
        ),
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    hierarchy_stale_grace_seconds: float = Field(
        default=120.0,
        gt=0,
        alias="AVA_HIERARCHY_STALE_GRACE_SECONDS",
        description=(
            "Grace beyond the job deadline before a `running` row counts as a "
            "dead process's leftover (the scan's stale-running sweep, "
            "seconds). The child gets its full deadline to finish or be killed "
            "by its parent; only after both are gone is the row garbage."
        ),
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    hierarchy_retry_backoff_seconds: float = Field(
        default=1800.0,
        gt=0,
        alias="AVA_HIERARCHY_RETRY_BACKOFF_SECONDS",
        description=(
            "Base delay before retrying or continuing an agent whose last "
            "build attempt was not clean. 30min: longer than a common "
            "provider outage (the failure a retry is for) and shorter than a "
            "typical compact cycle. Pure continuations (budget-truncated, no "
            "failures) drain immediately instead."
        ),
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    hierarchy_retry_backoff_cap_seconds: float = Field(
        default=86400.0,
        gt=0,
        alias="AVA_HIERARCHY_RETRY_BACKOFF_CAP_SECONDS",
        description=(
            "Ceiling of the exponential retry backoff (base * 2^(consecutive "
            "non-clean attempts)). A deterministic failure retries at most "
            "about once a day — cheap, self-healing when the cause clears, "
            "and superseded by the next compact's job anyway."
        ),
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    hierarchy_generation_concurrency: int = Field(
        default=12,
        ge=1,
        alias="AVA_HIERARCHY_GENERATION_CONCURRENCY",
        description=(
            "Parallel model calls within one build job. Default 12 mirrors "
            "the engine's DEFAULT_MAX_CONCURRENT (the SDK batch ceiling: one "
            "action must not become an account-wide request burst); the "
            "worker already serializes jobs, so this tunes only per-job burst."
        ),
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
