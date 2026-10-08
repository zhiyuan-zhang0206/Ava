"""Fleet policy and maintenance configuration; pure across agent and service faces."""

from pydantic import BaseModel, ConfigDict, Field

from base.packages.plugins.extensions import PluginContributions


class FleetConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_maintenance_enabled: bool = Field(
        default=True,
        description="Run the task-maintenance daemon on the gateway. On by default; set false to disable task reminders and the escalation pass cluster-wide.",
        json_schema_extra={
            "env_var": "AVA_TASK_MAINTENANCE_ENABLED",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "capability": "gateway",
            "scope": "host",
            "remote_writable": True,
        },
    )

    task_maintenance_interval_seconds: float = Field(
        default=300.0,
        description="Task-maintenance daemon poll interval (seconds): how often it checks for overdue tasks and reminds owners. A precision lower bound, not the cadence — each task controls its own remind_interval_seconds.",
        json_schema_extra={
            "env_var": "AVA_TASK_MAINTENANCE_INTERVAL_SECONDS",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "capability": "gateway",
            "scope": "cluster-pinned",
        },
    )

    task_reminder_backoff_seconds: float = Field(
        default=3600.0,
        description="Floor for the interval (seconds) between repeated reminders for the same overdue window: a task whose remind_interval_seconds exceeds this repeats at its own interval instead.",
        json_schema_extra={
            "env_var": "AVA_TASK_REMINDER_BACKOFF_SECONDS",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "capability": "gateway",
            "scope": "cluster-pinned",
        },
    )

    task_escalate_n: int = Field(
        default=3,
        description="Number of unanswered reminders before the daemon escalates to the parent task's owner.",
        json_schema_extra={
            "env_var": "AVA_TASK_ESCALATE_N",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "capability": "gateway",
            "scope": "cluster-pinned",
        },
    )

    reduce_context_switch: bool = Field(
        default=True,
        description=(
            "Platform-wide reduce-context-switch default (user ruling 2026-09-20): "
            "inject the 'Reduce context switch for the human' system-prompt section — "
            "queue-never-push, one notice per manager updated in place, milestone "
            "cadence, authorization/decision direct, out-of-band push only for a true "
            "emergency. False is the escape hatch back to the pre-platform behavior: "
            "the section is not injected and the reduce-context-switch policy keys "
            "do not apply."
        ),
        json_schema_extra={
            "env_var": "AVA_REDUCE_CONTEXT_SWITCH",
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "capability": "agent-runner",
        },
    )


def contribute() -> PluginContributions:
    """Declare Fleet's schema and its existing non-secret notification dependency."""
    return PluginContributions(config=FleetConfig, flags=("daemon.notice_ttl_limit_seconds",))
