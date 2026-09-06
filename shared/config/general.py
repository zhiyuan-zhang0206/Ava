"""General config — GeneralSettings.

Split out of the former flat Settings god object; each field keeps its exact
env alias so the .env surface is unchanged. Aggregated by shared/config.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger
from pydantic import Field, field_validator, model_validator

from shared.config._base import EnvSettings


class GeneralSettings(EnvSettings):
    ava_home: Path = Field(
        default=Path.home() / ".ava",
        alias="AVA_HOME",
        description="User data root — plugins, memory, and run state live underneath. Use a distinct value (e.g. ~/.ava-prod) per instance on one host.",
        json_schema_extra={
            "restart_required": "all",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    timezone: str = Field(
        default="America/Los_Angeles",
        alias="AVA_TIMEZONE",
        description="Timezone every agent-facing timestamp is rendered in, and the default for schedules an agent creates. Declared to the agent once as a standing context note, so the timestamps themselves carry no timezone suffix. Defaults to Los Angeles time.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    @field_validator("timezone", mode="before")
    @classmethod
    def _validate_timezone(cls, v: object) -> object:
        """Fail fast at Settings construction on an unparseable AVA_TIMEZONE.

        The envelope / exec-output / lifecycle timestamp paths call
        `ZoneInfo(settings.general.timezone)` with no try/except, so a bad
        timezone crashed the FIRST inbound turn of every agent until a human
        noticed — the config panel accepts any string for a str field, and
        nothing validated the value at write time. Probing ZoneInfo here moves
        the failure to process start (or to the config-write validation that
        reconstructs Settings), where it is loud and immediate.
        """
        if isinstance(v, str):
            try:
                ZoneInfo(v)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(
                    f"AVA_TIMEZONE={v!r} is not a valid IANA timezone name "
                    f"(e.g. America/Los_Angeles, Asia/Shanghai, UTC)"
                ) from exc
        return v

    @model_validator(mode="after")
    def _warn_when_timezone_unset(self) -> GeneralSettings:
        """A silent America/Los_Angeles default is this cluster's worst failure mode.

        The field default only applies when AVA_TIMEZONE is absent from both the
        process env and the unit .env. On a cluster pinned to another zone
        (2026-08-12 ruling: Asia/Shanghai) that fallback fires cron schedules at
        PT midnight instead of the cluster's midnight — schedule #3 did exactly
        that on 2026-08-21 while its .env lacked the key. The default is kept
        for explicit-PT installs, but a process that never received a value must
        not drift silently: name the missing key and the consequence."""
        if "timezone" not in self.model_fields_set:
            # loguru directly (not shared.log): this fires during the Settings
            # singleton build, before any process has installed the stdlib ->
            # loguru bridge, and loguru's default stderr sink keeps the warning
            # visible even then.
            logger.warning(
                "AVA_TIMEZONE is not set (env or unit .env) — "
                "settings.general.timezone falls back to the field default "
                "America/Los_Angeles. A schedule runner with this default fires "
                "cron jobs at PT midnight instead of the cluster's configured "
                "midnight (2026-08-21 incident, schedule #3). Set AVA_TIMEZONE "
                "in the cluster .env."
            )
        return self

    message_timestamps: bool = Field(
        default=True,
        alias="AVA_MESSAGE_TIMESTAMPS",
        description="Whether agent-facing messages carry a timestamp (inbound envelope, exec output headers, lifecycle markers). Turn off when the prefix is noise; the UI still shows its own timestamps.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    message_timestamp_weekday: bool = Field(
        default=False,
        alias="AVA_MESSAGE_TIMESTAMP_WEEKDAY",
        description="Whether the agent-facing timestamp includes the weekday (e.g. `Mon`). No effect when message_timestamps is off.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    track_branch: str = Field(
        default="main",
        alias="AVA_TRACK_BRANCH",
        description="Git branch this cluster tracks; `ava cluster update` pulls from origin/<this>. Override for preview/staging clusters.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    track_mode: Literal["latest", "releases"] = Field(
        default="latest",
        alias="AVA_TRACK_MODE",
        description="What `ava cluster update` converges to: `latest` = the tip of AVA_TRACK_BRANCH (default main); `releases` = the newest dated release tag (vX.Y.Z-YYYYMMDD[HHMM]) — main merges then deploy only at a release cut (scripts/release_cut.py --push).",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    os_jobs_enabled: bool = Field(
        default=True,
        alias="AVA_OS_JOBS_ENABLED",
        description="Whether this process may hand jobs to the platform scheduler (launchd LaunchAgent / crontab line / Task Scheduler task). The test suite turns it off: the scheduler is one namespace per OS user, so a test-scoped $AVA_HOME cannot isolate it. Deregistration is never gated.",
        json_schema_extra={
            "restart_required": "all",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    cluster_registry: Path = Field(
        default_factory=lambda: Path.home() / ".ava" / "clusters.json",
        alias="AVA_CLUSTER_REGISTRY",
        description="Host-level cluster registry file (name -> db/redis/ports), shared across all units on this host so parallel clusters see one index.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    machine_name: str = Field(
        default="",
        alias="AVA_MACHINE_NAME",
        description="Stable machine identifier for this host. Empty = fall back to the `$AVA_HOME/machine_name` file; neither set raises.",
        json_schema_extra={
            "restart_required": "all",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    machine_serve_gateway: bool | None = Field(
        default=None,
        alias="AVA_MACHINE_SERVE_GATEWAY",
        description="Whether this host serves the gateway capability (Postgres/Redis + HTTP gateway + gateway daemons). A host serves gateway and/or agent-runner; at least one must be true. None = fall back to the `$AVA_HOME/machine_serve_gateway` file.",
        json_schema_extra={
            "restart_required": "all",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    machine_serve_agent_runner: bool | None = Field(
        default=None,
        alias="AVA_MACHINE_SERVE_AGENT_RUNNER",
        description="Whether this host serves the agent-runner capability (agent host, ops server, and watchdog). A host serves gateway and/or agent-runner; at least one must be true. None = fall back to the `$AVA_HOME/machine_serve_agent_runner` file.",
        json_schema_extra={
            "restart_required": "all",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    machine_serve_observability_station: bool | None = Field(
        default=None,
        alias="AVA_MACHINE_SERVE_OBSERVABILITY_STATION",
        description="Whether this host serves the observability-station capability (owns the native LGTM observability backends — the declarative form of the `$AVA_HOME/lgtm-host` marker). A host serves gateway and/or agent-runner and/or observability-station; at least one must be true. None = fall back to the `$AVA_HOME/machine_serve_observability_station` file.",
        json_schema_extra={
            "restart_required": "all",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    machine_description: str = Field(
        default="",
        alias="AVA_MACHINE_DESCRIPTION",
        description="Free-text note of what this host is for, surfaced to agents via ava.self.MACHINE_SPEC and ava.agents.list_machines(). Empty = fall back to the `$AVA_HOME/machine_description` file; absence is legal.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    machine_host: str = Field(
        default="",
        alias="AVA_MACHINE_HOST",
        description="This host's reachable address — the IP/hostname other nodes and the browser dial it at, and the address authenticated Postgres, its pooler and Linux Redis bind in addition to loopback (macOS Redis retains its loopback relay). Empty (default): fall back to the `$AVA_HOME/machine_host` file, then `localhost`. Left empty on purpose — a `localhost` default here would shadow the file and register an enrolled agent-runner at a self-dialing address. A split deployment sets each node's real private-network address.",
        json_schema_extra={
            "restart_required": "all",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    memory_remote: str = Field(
        default="",
        alias="AVA_MEMORY_REMOTE",
        description="Central git remote URL for the memory pool (each host pushes to a machine-<name> branch). Empty = fall back to the `$AVA_HOME/memory_remote` file; neither set raises. Ignored when AVA_MEMORY_KEEP_LOCAL is true.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    memory_keep_local: bool = Field(
        default=False,
        alias="AVA_MEMORY_KEEP_LOCAL",
        description="Keep the memory pool purely local: no git remote, no push/pull, notes never leave the box. Default off (the pool syncs to AVA_MEMORY_REMOTE). Set true on a host whose notes must not sync off-box; explicit `ava memory init` then strips any existing remote and skips the GitHub PR capability check.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    cross_machine_transfer_backend: Literal["drive", "none"] = Field(
        default="drive",
        alias="AVA_CROSS_MACHINE_TRANSFER_BACKEND",
        description="Cross-machine file transfer backend on an agent-runner: `drive` probes a writable Google Drive synced folder at start and uses it when present (missing = warn, never block); `none` assumes no framework-managed backend. Auto-skipped on a single box.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    require_github_pr: bool = Field(
        default=True,
        alias="AVA_REQUIRE_GITHUB_PR",
        description="Require gh-authenticated GitHub PR access to the memory repo on an agent-runner at start (nightly memory consolidation). Auto-skipped on a single box. Set false to opt out on a split agent-runner that doesn't consolidate via PRs.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )
