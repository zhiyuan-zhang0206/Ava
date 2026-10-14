"""`Clock` and `Timers`: the injected handles to the cluster's wall clock and to elapsed time.

One timezone governs the whole cluster (user ruling 2026-08-27): every agent runner takes it from
the gateway and must not fall back to its own machine's zone. A composition root builds the
handle once (`Clock.from_settings()`); components take the `Clock`, never the settings. The
handle carries the three facts a time decision reads: the cluster timezone name, whether that
name is authoritative for this process (set at boot, not the field default), and whether
agent-facing timestamps carry the weekday. `now()` is the injectable time source.

`Timers` is the port for the other half of time: measuring elapsed time and waiting. A component
that must be driven by a test takes `timers: Timers` as a required argument (no default) and
calls `timers.monotonic()`, `timers.sleep(s)` or `await timers.asleep(s)` instead of the `time`
and `asyncio` functions. The composition root passes `SYSTEM_TIMERS`; a test passes
`base.clock.tests.manual_timers.ManualTimers`, which never waits and moves a virtual clock
(its `now()` is a wall clock that moves with it, for `Clock(config, timers=timers, now=timers.now)`). A
component that needs only elapsed time takes `Timers` and so needs no timezone configuration;
one that reads the wall clock takes the `Clock`, which carries a `Timers` of its own: it is built
as `Clock(config, timers=SYSTEM_TIMERS)` by a root and as `Clock(config, timers=manual,
now=manual.now)` by a test, and then satisfies `Timers` itself, so one object is passed all the
way down. `Clock.from_settings()` is the production constructor and uses `SYSTEM_TIMERS`. Not to
be confused with `base.deploy.timing.Clock`, the registry of clock-lattice constants.

`base.config.apply_cluster_timezone()` and `host_tz_name()` stay in `base.config`: they act on
the process (its `TZ`) or on the host, not on a handle.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from base.config import ConfigBoot, cluster_tz_name, settings


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


def clock_config_from_boot(boot: ConfigBoot) -> ClockConfig:
    """Read the operation's clock facts from its composition owner's live view."""
    return ClockConfig(
        timezone=boot.view.general.timezone,
        authoritative_timezone=(
            boot.view.general.timezone if boot.field_explicitly_set("timezone") else None
        ),
        message_timestamp_weekday=boot.view.general.message_timestamp_weekday,
    )


class Timers(Protocol):
    """Elapsed time and waiting: what a component reads to measure a duration or to pause.

    `monotonic()` is a point on a clock that never goes backwards; only differences between two
    readings mean anything. `sleep` blocks the calling thread for `seconds` and `asleep` suspends
    the calling task without blocking the event loop. A component that takes a `Timers` has no
    other source of elapsed time, so a test controls it completely.
    """

    def monotonic(self) -> float:
        """Seconds on a clock that only moves forward."""
        ...

    def sleep(self, seconds: float) -> None:
        """Block the calling thread for `seconds`."""
        ...

    async def asleep(self, seconds: float) -> None:
        """Suspend the calling task for `seconds`."""
        ...


class _SystemTimers:
    """The process's real timers: `time.monotonic`, `time.sleep` and `asyncio.sleep`."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    async def asleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


SYSTEM_TIMERS: Timers = _SystemTimers()
"""The real timers; a composition root passes this to every component that takes a `Timers`."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Clock:
    """The cluster wall clock, built from one `ClockConfig`, and the `Timers` it hands on.

    It satisfies `Timers` by delegating to the `timers` it was built with, so one object carries
    both the wall clock and elapsed time. A composition root passes `SYSTEM_TIMERS`; a test passes
    a `ManualTimers` and usually `now=manual.now`, so wall time and elapsed time share one
    virtual source.
    """

    def __init__(
        self,
        config: ClockConfig,
        *,
        timers: Timers,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._config = config
        self._timers = timers
        self._now = now

    @classmethod
    def from_settings(cls) -> Clock:
        """The production constructor: the config as the live settings hold it now, and the
        system's timers."""
        return cls(clock_config_from_settings(), timers=SYSTEM_TIMERS)

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
        `agent/graph/prompt/context_notes.py`. This is the single agent-facing timestamp
        representation: every producer goes through here.
        """
        local = dt.astimezone(self.explicit_zone())
        if self._config.message_timestamp_weekday:
            return local.strftime("[%Y-%m-%d %a %H:%M:%S]")
        return local.strftime("[%Y-%m-%d %H:%M:%S]")

    def now_timestamp(self) -> str:
        """The current time as an agent-facing timestamp string."""
        return self.format_timestamp(self.now())

    def monotonic(self) -> float:
        """`Timers.monotonic` of the timers this clock was built with."""
        return self._timers.monotonic()

    def sleep(self, seconds: float) -> None:
        """`Timers.sleep` of the timers this clock was built with."""
        self._timers.sleep(seconds)

    async def asleep(self, seconds: float) -> None:
        """`Timers.asleep` of the timers this clock was built with."""
        await self._timers.asleep(seconds)
