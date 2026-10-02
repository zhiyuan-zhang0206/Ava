"""`Clock`: the injected handle to the cluster's wall clock.

One timezone governs the whole cluster (user ruling 2026-08-27): every agent runner takes it from
the gateway and must not fall back to its own machine's zone. A composition root builds the
handle once (`Clock.from_settings()`); components take the `Clock`, never the settings. The
handle carries the three facts a time decision reads: the cluster timezone name, whether that
name is authoritative for this process (set at boot, not the field default), and whether
agent-facing timestamps carry the weekday. `now()` is the injectable time source.

`base.config.apply_cluster_timezone()` and `host_tz_name()` stay in `base.config`: they act on
the process (its `TZ`) or on the host, not on a handle.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from base.config import cluster_tz_name, settings


@dataclass(frozen=True)
class ClockConfig:
    """What the clock decides on, read from settings in one place."""

    # The cluster-pinned timezone as the field holds it (the default when none was set).
    timezone: str
    # The same name when it was explicitly provided to this process (env / unit `.env` /
    # bootstrap fetch), else None: the host-zone fallback signal of a settings-lite process.
    authoritative_timezone: str | None
    message_timestamp_weekday: bool


def clock_config_from_settings() -> ClockConfig:
    """Build the slice from the live settings (read at each call, like the other kernel slices)."""
    return ClockConfig(
        timezone=settings.general.timezone,
        authoritative_timezone=cluster_tz_name(),
        message_timestamp_weekday=settings.general.message_timestamp_weekday,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Clock:
    """The cluster wall clock, built from one `ClockConfig`."""

    def __init__(self, config: ClockConfig, *, now: Callable[[], datetime] = _utc_now) -> None:
        self._config = config
        self._now = now

    @classmethod
    def from_settings(cls) -> Clock:
        """The composition-root constructor: the config as the live settings hold it now."""
        return cls(clock_config_from_settings())

    @property
    def timezone(self) -> str:
        """The cluster timezone name (the field default when none was set)."""
        return self._config.timezone

    @property
    def authoritative_timezone(self) -> str | None:
        """The cluster timezone name when this process holds an authoritative one, else None."""
        return self._config.authoritative_timezone

    def now(self) -> datetime:
        """The current time, timezone-aware UTC."""
        return self._now()

    def zone(self) -> ZoneInfo | None:
        """The authoritative cluster zone, or None when this process holds none.

        None is the host-zone fallback signal: `dt.astimezone(None)` is machine-local, the
        documented degradation of a maintenance verb running while the gateway is down. A name
        that fails to parse as IANA also yields None rather than crashing a display path.
        """
        name = self._config.authoritative_timezone
        if name is None:
            return None
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            return None

    def explicit_zone(self) -> ZoneInfo:
        """The cluster zone by name, for callers that need a zone object even when no
        authoritative value was set (the field default then applies)."""
        return ZoneInfo(self._config.timezone)

    def format_timestamp(self, dt: datetime) -> str:
        """Render a TZ-aware datetime as the agent-facing timestamp string.

        Format `[YYYY-MM-DD HH:MM:SS]`, with the weekday abbreviation between date and time when
        `message_timestamp_weekday` is on. `dt` is converted to the cluster zone first, so values
        read back from the database (TIMESTAMPTZ / UTC) render in the same wall clock as current
        stamps. No zone suffix: the zone is cluster-pinned, so a suffix would be a constant
        repeated on every timestamp (and ambiguous: `%Z` gives PDT/PST across DST, and CST names
        two zones); the agent is told the zone once by the standing context note in
        `agent/graph/context_notes.py`. This is the single agent-facing timestamp
        representation: every producer goes through here.
        """
        local = dt.astimezone(self.explicit_zone())
        if self._config.message_timestamp_weekday:
            return local.strftime("[%Y-%m-%d %a %H:%M:%S]")
        return local.strftime("[%Y-%m-%d %H:%M:%S]")

    def now_timestamp(self) -> str:
        """The current time as an agent-facing timestamp string."""
        return self.format_timestamp(self.now())
