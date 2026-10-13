"""What each public watcher call asks for, as plain values.

`launch`, `cron` and `at` in `ava/watcher/__init__.py` coerce their arguments and read the
clock and the configured timezones; the functions here turn those into a `SpawnRequest`. They
take the current time and the timezone defaults as arguments, so a test fixes both and asserts
on the request without starting a session.
"""

from __future__ import annotations

import datetime

from ava.sdk_surface.validation import coerce_str
from ava.watcher.spawn import SpawnRequest
from base.daemon.schedules.watcher import (
    DEFAULT_STANDING_CRON_MAX_SECONDS,
    build_at_script,
    build_cron_script,
    normalize_when,
    parse_timeout,
    validate_cron,
    validate_timezone,
)

__all__ = ["plan_at", "plan_cron", "plan_launch"]


def validate_message(message: str) -> None:
    if not message.strip():
        raise ValueError("message cannot be empty")


def plan_launch(
    code: str, timeout: float | datetime.timedelta | str, name: str, notify: str | None
) -> SpawnRequest:
    """A background program bounded by `timeout`; exits with code 124 when it runs out."""
    code = coerce_str(code, "code")
    timeout = coerce_str(timeout, "timeout", allow_types=(int, float, datetime.timedelta))
    name = coerce_str(name, "name")
    notify = coerce_str(notify, "notify", allow_none=True)
    seconds = parse_timeout(timeout)
    return SpawnRequest(
        code=code,
        watchdog_secs=seconds,
        name=name,
        kind="launch",
        timeout_secs=seconds,
        notify=notify,
    )


def plan_cron(
    expr: str,
    message: str,
    *,
    timezone: str | None,
    end_time: datetime.datetime | datetime.timedelta | str | None,
    name: str,
    notify: str | None,
    now: datetime.datetime,
    default_timezone: str,
) -> SpawnRequest:
    """A cron wake-up that runs until `end_time` (default: seven days from the current minute).

    `default_timezone` is the cluster clock's zone, or this host's zone for a settings-lite
    process; it applies when `timezone` is None.

    Raises:
        ValueError: the message is empty, or `end_time` is in the past.
    """
    expr = coerce_str(expr, "expr")
    message = coerce_str(message, "message")
    timezone = coerce_str(timezone, "timezone", allow_none=True)
    end_time = coerce_str(
        end_time, "end_time", allow_none=True, allow_types=(datetime.datetime, datetime.timedelta)
    )
    name = coerce_str(name, "name")
    notify = coerce_str(notify, "notify", allow_none=True)
    validate_message(message)
    validate_cron(expr)
    tz = timezone if timezone is not None else default_timezone
    validate_timezone(tz)
    if end_time is None:
        # Standing-cron cap (task #2617): no end_time means the DEFAULT
        # window - 7 days, counted from the current minute - not forever. A
        # longer schedule must pass an explicit end_time.
        et = now.replace(second=0, microsecond=0) + datetime.timedelta(
            seconds=DEFAULT_STANDING_CRON_MAX_SECONDS
        )
    else:
        # normalize_when (not normalize_end_time): this branch excludes None,
        # so the result is a non-optional datetime for the type checker.
        et = normalize_when(end_time)
        # Align with at(): a past end must not register.
        if et < now:
            raise ValueError(
                f"end_time is in the past: {et.isoformat()}. "
                "Provide a future time, or use a positive timedelta."
            )
    code = build_cron_script(
        expr=expr,
        message=message,
        timezone=tz,
        end_time_iso=et.isoformat(),
    )
    # The generated script self-terminates (it stops looping past end_time -
    # which every registration now carries, the default being now + 7 days),
    # so no watchdog.
    return SpawnRequest(
        code=code, watchdog_secs=None, name=name, kind="cron", cron_end_at=et, notify=notify
    )


def plan_at(
    when: datetime.datetime | datetime.timedelta | str,
    message: str,
    *,
    name: str,
    notify: str | None,
    now: datetime.datetime,
    announce_timezone: str | None,
) -> SpawnRequest:
    """A one-shot wake-up at `when`.

    The announcement's wall clock follows `announce_timezone` (the cluster timezone; None for
    a settings-lite process, which renders in the watcher's own wall clock); the sleep is
    UTC-based regardless.

    Raises:
        ValueError: the message is empty, or `when` is in the past.
    """
    when = coerce_str(when, "when", allow_types=(datetime.datetime, datetime.timedelta))
    message = coerce_str(message, "message")
    name = coerce_str(name, "name")
    notify = coerce_str(notify, "notify", allow_none=True)
    validate_message(message)
    due_at = normalize_when(when)
    if due_at < now:
        raise ValueError(
            f"when is in the past: {due_at.isoformat()}. "
            "Provide a future time, or use a positive timedelta."
        )
    code = build_at_script(
        when_iso=due_at.isoformat(),
        message=message,
        timezone=announce_timezone,
    )
    # The one-shot script sleeps until `when`, wakes you once, and exits - it
    # ends itself, so no watchdog.
    return SpawnRequest(
        code=code, watchdog_secs=None, name=name, kind="at", fires_at=due_at, notify=notify
    )
