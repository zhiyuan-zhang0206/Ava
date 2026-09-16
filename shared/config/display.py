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

    run_timeline_layers_max_nodes: int = Field(
        default=200,
        alias="AVA_RUN_TIMELINE_LAYERS_MAX_NODES",
        description=(
            "Max narrative-layer nodes one depth level may contribute to "
            "GET /api/agents/{id}/run-timeline's layers. When the finest level's "
            "intersecting nodes exceed it, the merge drops to the next coarser "
            "level until the row fits — 200 blocks in one chart row is already "
            "dense, and nothing is lost: the coarser levels summarize the same "
            "window."
        ),
        json_schema_extra={
            "restart_required": "gateway",
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
