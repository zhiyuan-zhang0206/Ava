"""Display config — DisplaySettings.

User-facing window and pagination defaults: how many items a list surface
returns by default, how far a history window may page, and the fetch sizes
the web UI uses when walking a timeline (task #3696, user ruling 2026-09-17:
every user-facing limit is configurable and each carries its reason).

Fields land in this module together with their consumers, one batch per pull
request. A field's ``description`` states the reason for its default value,
and its ``restart_required`` names the process kind that must be restarted
after a change.
"""

from __future__ import annotations

from pydantic import Field

from shared.config._base import EnvSettings


class DisplaySettings(EnvSettings):
    """User-facing listing/window defaults (see the module docstring)."""

    messages_default_limit: int = Field(
        default=100,
        alias="AVA_MESSAGES_DEFAULT_LIMIT",
        description=(
            "Default tail window (items) of GET /api/agents/{id}/messages when the caller "
            "passes no limit. 100 covers the recent slice an implicit read usually wants "
            "(tens of turns once each turn's reasoning/code/output blocks are counted) "
            "without shipping a whole long conversation; callers that need more pass an "
            "explicit limit (1..10000)."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
    timeline_default_limit: int = Field(
        default=50,
        alias="AVA_TIMELINE_DEFAULT_LIMIT",
        description=(
            "Default timeline tail-window (items) for GET /api/agents/{id}/timeline, the "
            "agent-published timeline snapshot trim, and the CLI timeline command. The unit "
            "is timeline items (one turn fans out into reasoning/code/output items), so 50 "
            "is several screenfuls; both producers share it so a streaming turn always lands "
            "inside the window the frontend already holds."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    notices_open_default_limit: int = Field(
        default=200,
        alias="AVA_NOTICES_OPEN_DEFAULT_LIMIT",
        description=(
            "Default cap on one open-notices read (GET /api/notices/open, /api/notices/live, "
            "and the unified feed's open/awaiting slices) when the caller passes no limit. "
            "200 covers a large fleet backlog in one request while bounding the poll payload "
            "the IM bridge fans out to Telegram and the CLI's fleet-wide listing; the "
            "protective ceiling (500) stays a constant."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
    notices_resolved_default_page: int = Field(
        default=30,
        alias="AVA_NOTICES_RESOLVED_DEFAULT_PAGE",
        description=(
            "One resolved-notices history page (GET /api/notices/resolved and the unified "
            "feed's resolved_limit) when the caller passes none. 30 is a screenful of greyed "
            "history per keyset page (deepening is a cursor fetch, not a bigger page); the "
            "protective ceiling (100) stays a constant."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    shell_capture_default_lines: int = Field(
        default=200,
        alias="AVA_SHELL_CAPTURE_DEFAULT_LINES",
        description=(
            "Default tail window (lines) of a shell capture when the caller passes none: "
            "the shell monitor page's poll (GET /api/agents/{id}/shell/{sid}), the SDK's "
            "ava.shell.sessions.capture(), and the pty CLI's bare `capture` op. 200 lines "
            "is a few screenfuls and matches the monitor page's own default; the valid "
            "range (50..2000 at the API, hard clamp 100000 at the pty host) stays fixed "
            "(protective constants, task #3696 exception inventory)."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    timeline_history_page_base: int = Field(
        default=50,
        alias="AVA_TIMELINE_HISTORY_PAGE_BASE",
        description=(
            "Base page (items) the web UI fetches per timeline scroll-up; each successive "
            "scroll-up doubles it (50, 100, 200, ... up to the endpoint's 1000 le). Read at "
            "runtime from GET /api/config by the frontend; 50 keeps the first backfill "
            "light while the doubling keeps deep histories a few fetches away."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    events_default_limit: int = Field(
        default=100,
        alias="AVA_EVENTS_DEFAULT_LIMIT",
        description=(
            "Default page (events) of the agent event-history reads — GET "
            "/api/agents/{id}/events and GET /api/events — when the caller passes no "
            "limit. 100 rows covers a debugging glance over the recent activity without "
            "having the gateway parse a long Loki slice; callers that need more pass an "
            "explicit limit (1..1000)."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    alerts_default_limit: int = Field(
        default=100,
        alias="AVA_ALERTS_DEFAULT_LIMIT",
        description=(
            "Default page (alerts) of GET /api/alerts when the caller passes no limit. "
            "100 rows covers the unresolved-first view a dashboard read wants without "
            "shipping the full resolution history; the 500 ceiling stays a protective "
            "constant, and the web list's own pinned window is tracked in the task "
            "#3696 user-flag list (deferred backend alignment)."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
    schedules_runs_default_limit: int = Field(
        default=50,
        alias="AVA_SCHEDULES_RUNS_DEFAULT_LIMIT",
        description=(
            "Default page (run rows) of GET /api/schedules/{id}/runs when the caller "
            "passes no limit. 50 rows spans a month of daily runs (a few days of "
            "frequent ones) - the recent-pattern glance the runs view opens with; the "
            "CLI passes its own explicit limit and is unaffected."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
    config_audit_default_last: int = Field(
        default=20,
        alias="AVA_CONFIG_AUDIT_DEFAULT_LAST",
        description=(
            "Default `last` (records) of GET /api/config/audit when omitted. 20 recent "
            "config writes is the recent-activity glance the audit view opens with; the "
            "1..200 range and its 200 ceiling stay protective constants."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    cluster_events_default_limit: int = Field(
        default=200,
        alias="AVA_CLUSTER_EVENTS_DEFAULT_LIMIT",
        description=(
            "Default page (rows) of GET /api/cluster/admin/events when the caller "
            "passes no limit. 200 rows is the ops-debugging glance over the unified "
            "stream; the handler's [1, 1000] range stays a protective constant."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
    metrics_default_window_days: int = Field(
        default=1,
        alias="AVA_METRICS_DEFAULT_WINDOW_DAYS",
        description=(
            "Default window (days) of GET /api/metrics and /api/metrics/agents when "
            "the caller passes none. 1 day is the settings tab's default read; the "
            "30-day cap stays a protective constant (it bounds the Loki scan)."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    neighbors_default_depth: int = Field(
        default=1,
        alias="AVA_NEIGHBORS_DEFAULT_DEPTH",
        description=(
            "Default tie-walk depth of GET /api/agents/{id}/neighbors and the SDK's "
            "ava.agents.get_neighbors when the caller passes none. 1 returns direct "
            "ties only - the inspector's default view; each extra hop discounts tie "
            "strength, and the 5-hop ceiling stays a protective constant."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
    neighbors_default_limit: int = Field(
        default=20,
        alias="AVA_NEIGHBORS_DEFAULT_LIMIT",
        description=(
            "Default cap on the neighbors returned (strongest first) by the same "
            "reads when the caller passes none. 20 fills the relationship view "
            "without dragging a whole fleet's tie list into one response; the "
            "100 ceiling stays a protective constant."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
