"""Run temporary background waits that can wake you with a message.

A watcher continues after you end a turn or terminate, and its next message can
wake you again. Stop unwanted watchers before ending their task. Watchers are
never restarted automatically after their execution stops; use a schedule for
recurring work that must recover from interruptions.

Completion reports follow the notification policy and are not liveness signals.
An explicit cancellation sends no additional report; an abrupt machine failure
may leave no report. Inspect recorded waits when resuming work.
"""

from __future__ import annotations

import datetime

from base.daemon.schedules.watcher import (
    CronExprError as CronExprError,
)
from meiva.watcher import plans, spawn, wiring

__all_for_meiva__ = [
    "at",
    "cron",
    "launch",
]

type WatcherTimeout = float | datetime.timedelta | str


def launch(code: str, timeout: WatcherTimeout, *, name: str, notify: str | None = None) -> int:
    """Run `code` as a background watcher, bounded by `timeout`.

    `code` calls `meiva.agents.send_message(meiva.self.AGENT_ID, content)` to wake you;
    the watcher runs until its exit, your kill, or `timeout`. On exit, its code
    and output are available through the completion policy; timeout exits with code 124.

    Args:
        timeout: seconds, a `timedelta`, or `"<n>{s,m,h,d}"` (e.g. `"30m"`).
        name: a lowercase slug like `"ci-monitor"`.
        notify: omit to use the agent policy; `"always"` delivers every completion immediately.
    Returns:
        The watcher's session id — it is one of your shell sessions while running.
    """
    request = plans.plan_launch(code, timeout, name, notify)
    return spawn.run(request, wiring.spawn_deps())


def cron(
    expr: str,
    message: str,
    *,
    timezone: str | None = None,
    end_time: datetime.datetime | datetime.timedelta | str | None = None,
    name: str,
    notify: str | None = None,
) -> int:
    """Runs until `end_time`, or until you kill its session.

    `end_time` defaults to now + 7 days; pass an explicit one for a longer
    schedule. Calling `cron()` again with the same expression + timezone does
    NOT replace anything — it starts another, independent session; if you
    want to renew or replace a schedule, kill the old session yourself
    (`meiva.shell.sessions.kill`) before or after registering the new one.

    Args:
        expr: 5-field cron expression (`minute hour day-of-month month
            day-of-week`).
        timezone: IANA name; defaults to your configured timezone.
        end_time: same accepted types as `at()`'s `when`; must be in the future.
        name: a lowercase slug like `"daily-check-in"`.
        notify: omit to use the agent policy; `"always"` delivers every completion immediately.
    Returns:
        The watcher's session id; kill it to stop the schedule.
    """
    from base.clock import Clock
    from base.config import host_tz_name

    # Default to the cluster clock when authoritative; a settings-lite
    # process (no authoritative cluster timezone) falls back to this host's
    # own zone - the same wall clock its other displays use.
    request = plans.plan_cron(
        expr,
        message,
        timezone=timezone,
        end_time=end_time,
        name=name,
        notify=notify,
        now=datetime.datetime.now(datetime.UTC),
        default_timezone=Clock.from_settings().authoritative_timezone or host_tz_name(),
    )
    return spawn.run(request, wiring.spawn_deps())


def at(
    when: datetime.datetime | datetime.timedelta | str,
    message: str,
    *,
    name: str,
    notify: str | None = None,
) -> int:
    """Wake you once at a future time, then finish the watcher.

    Args:
        when: a TZ-aware datetime, a timedelta from now (UTC), or an ISO-8601
            string with timezone. Must be in the future.
        name: a lowercase slug like `"stand-up-reminder"`.
        notify: omit to use the agent policy; `"always"` delivers every completion immediately.

    Returns:
        The watcher's session id; kill that session to cancel.
    """
    from base.clock import Clock

    request = plans.plan_at(
        when,
        message,
        name=name,
        notify=notify,
        now=datetime.datetime.now(datetime.UTC),
        announce_timezone=Clock.from_settings().authoritative_timezone,
    )
    return spawn.run(request, wiring.spawn_deps())
