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
