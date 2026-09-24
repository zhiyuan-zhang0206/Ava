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
builds through `services.hierarchy_worker`. The generation model is the
target agent's own effective model
(`shared.agent_snapshot.agent_effective_model` — overlay preferred, fleet
default else), with `settings.lm.hierarchy_model` as the last-resort fallback.
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

    hierarchy_tail_seal_enabled: bool = Field(
        default=False,
        alias="AVA_HIERARCHY_TAIL_SEAL_ENABLED",
        description=(
            "Enable the tail-seal channel (task #3981 C): idle agents' "
            "trailing stretch is sealed so default run-timeline windows show "
            "real layer blocks. Ships dark (False); the pilot gates the "
            "switch-on (its report + the in-loop user are notified first)."
        ),
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    hierarchy_tail_idle_minutes: float = Field(
        default=15.0,
        gt=0,
        alias="AVA_HIERARCHY_TAIL_IDLE_MINUTES",
        description=(
            "Idle gate (minutes): a tail seal is due only after the agent's "
            "newest checkpoint has been quiet this long. 15min sits inside "
            "the 30min default window's geometry (after a stop, the window's "
            "right half becomes a real block) and avoids sealing a stretch "
            "that merely paused mid-turn."
        ),
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    hierarchy_tail_min_interval_minutes: float = Field(
        default=60.0,
        gt=0,
        alias="AVA_HIERARCHY_TAIL_MIN_INTERVAL_MINUTES",
        description=(
            "Minimum gap (minutes) between tail seals for one agent — the "
            "cost ceiling (<=24 seals/day/agent). A clean continuation "
            "(budget-truncated, no failures) drains immediately instead."
        ),
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    hierarchy_tail_max_per_tick: int = Field(
        default=3,
        ge=1,
        alias="AVA_HIERARCHY_TAIL_MAX_PER_TICK",
        description=(
            "Most tail jobs one scan tick may enqueue. The serial worker "
            "drains the minute-tick queue quickly; the cap prevents one tick "
            "from stacking a burst of heavy builds behind a quiet stretch."
        ),
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    hierarchy_worker_enabled: bool = Field(
        default=False,
        alias="AVA_HIERARCHY_WORKER_ENABLED",
        description=(
            "Master switch for the event-driven build trigger (task #4674). "
            "Off ships the new shape dark: the compact-boundary enqueue and "
            "the worker's tick both no-op, so nothing builds until alignment "
            "with the user turns it on (the schedule's enabled flag stays the "
            "operational layer beneath this code gate)."
        ),
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    hierarchy_fallback_scan_seconds: float = Field(
        default=600.0,
        gt=0,
        alias="AVA_HIERARCHY_FALLBACK_SCAN_SECONDS",
        description=(
            "Period (seconds) of the reconcile scan behind the event trigger "
            "(task #4674): the old full scan still runs this often to repair "
            "lost events, retry non-clean attempts once their backoff has "
            "elapsed, and drain budget continuations. The worker's first tick "
            "after a process start always scans. 10min: retry pacing is "
            "already bounded by the 30min backoff base, and a scan is two "
            "aggregated queries, so a tighter cadence would buy nothing."
        ),
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    hierarchy_regen_alert_nodes_per_job: int = Field(
        default=150,
        ge=1,
        alias="AVA_HIERARCHY_REGEN_ALERT_NODES_PER_JOB",
        description=(
            "Alert threshold on one job's generated node count (task #4674 "
            "guardrail — observability only, never blocking). Calibrated "
            "2026-09-24 against the fleet: ordinary compact-driven jobs "
            "(n=814) run p50=12 / p95=21 / max=34, and every value observed "
            "above 150 belonged to the exempt face (a first build); the "
            "incident (a reader fix invalidated every input hash) generated "
            "500-1200 per job. 150 = ~4.4x the observed normal max, with "
            "zero non-exempt hits."
        ),
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    hierarchy_regen_halt_nodes_per_job: int = Field(
        default=400,
        ge=1,
        alias="AVA_HIERARCHY_REGEN_HALT_NODES_PER_JOB",
        description=(
            "Mid-run stop: once a job's generated count reaches this, the "
            "remaining nodes are left unattempted (skipped) and the job ends "
            "with a halt marker, so its continuation waits out the retry "
            "backoff instead of hot-looping (task #4674 guardrail). "
            "Calibrated 2026-09-24: 400 = ~12x the observed normal max (34, "
            "n=814) and sits low inside the incident's 500-1200 band, so a "
            "re-cut wave is cut within its first minutes; first builds and "
            "tail seals are exempt (measured worst: 1370, the 9/17 wave; "
            "1109 on 9/21)."
        ),
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    hierarchy_regen_daily_budget_nodes: int = Field(
        default=12000,
        ge=1,
        alias="AVA_HIERARCHY_REGEN_DAILY_BUDGET_NODES",
        description=(
            "Fleet-wide 24h rolling budget on generated nodes; crossing it "
            "trips the persistent breaker (hierarchy_worker_breaker) and the "
            "worker stops claiming until an operator explicitly resets it "
            "with a note (task #4674 guardrail). Calibrated 2026-09-24: "
            "normal days run 3424-6256 (9/18-9/22 — a 5000 budget would "
            "have false-tripped three of them), so 12000 = ~2x the observed "
            "peak; a full-fleet re-cut is >=35559 (80 agent trees, max "
            "single tree 3169), so the breaker fires about a third of the "
            "way in, and the incident's 5.5k/h build rate crosses it in ~2h."
        ),
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    hierarchy_regen_min_reuse_ratio: float = Field(
        default=0.5,
        gt=0,
        lt=1,
        alias="AVA_HIERARCHY_REGEN_MIN_REUSE_RATIO",
        description=(
            "Alert threshold on one job's reused/(reused+generated) ratio "
            "(task #4674 guardrail — observability only). Its face is paired "
            "with the size threshold (`generated > "
            "hierarchy_regen_alert_nodes_per_job` as well): calibrated "
            "2026-09-24, the unpaired ratio fires on 111/814 normal jobs "
            "(all fresh slices under 50 nodes), while the pair fires 0/814 "
            "normally and 15/24 on the incident's big items. First builds "
            "and tail seals are exempt."
        ),
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
