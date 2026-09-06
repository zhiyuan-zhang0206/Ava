"""Watcher shared helpers — cron validation / next-fire, `when` normalization,
and watcher-script generation.

Used by the `ava.watcher` SDK. A time watcher is just a normal session running a
generated Python script that sleeps until its target time(s) and delivers its
message back to the launching agent (a `watcher:N`-tagged chat inbound via the
gateway client); the builders here produce those scripts. The cron math
(`validate_cron` / `next_fire`) and the `when` normalization were previously in
`shared/schedule.py` / `ava/schedule.py`, relocated here when the central
scheduler was removed.
"""

from __future__ import annotations

import datetime as _dt
import textwrap as _tw
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter


class CronExprError(Exception):
    """Invalid cron expression."""


__all__ = [
    "TEMPLATE_VERSION",
    "CronExprError",
    "build_at_script",
    "build_cron_script",
    "next_fire",
    "normalize_end_time",
    "normalize_when",
    "previous_fire",
    "validate_cron",
    "validate_timezone",
]


# Template version: bumped whenever a generated watcher script's loop
# semantics change (issue #1330). The registry stores the version a session was
# spawned with; the boot reconcile rebuilds a live cron watcher whose version is
# behind, so a template fix reaches watchers that were already running when it
# landed (the generated script is frozen at launch — a rollout does not rewrite
# it). v1 = pre-#182 loop (no rollback guard); v2 = #182 loop (_last guard +
# boundary re-check); v3 = schedule-state announcement prints (a healthy cron
# watcher sleeping toward its next fire was indistinguishable from a stuck one —
# 2026-08-25 false alarm, task #1620); v4 = orphan guard (a watcher child
# hard-exits within seconds of its pty host dying — task #1726, 49/85 watchers
# were multi-generation orphans still firing cron/at).
TEMPLATE_VERSION = 4


# Cron


def validate_timezone(tz: str) -> None:
    """Validate an IANA timezone string (e.g. ``"America/Los_Angeles"``).

    Raises ValueError when the timezone name is not recognized.
    """
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, KeyError) as exc:
        raise ValueError(f"invalid IANA timezone: {tz!r}") from exc


def validate_cron(expr: str) -> None:
    """Validate a 5-field cron expression; raises CronExprError on invalid."""
    try:
        croniter(expr, _dt.datetime.now(_dt.UTC))
    except (ValueError, KeyError) as exc:
        raise CronExprError(f"invalid cron expression: {expr!r}") from exc


def next_fire(
    expr: str,
    after: _dt.datetime | None = None,
    timezone: str | None = None,
    tolerance: _dt.timedelta | None = None,
) -> _dt.datetime:
    """Given a cron expression + base time (default now UTC), return the next
    fire time (UTC).

    ``timezone`` is an IANA timezone string (e.g. ``"Asia/Shanghai"``). None ->
    compute in UTC. croniter parses the cron expression in this timezone.

    The UTC default is the primitive staying neutral: it has no opinion about
    which wall clock a caller means, and UTC is the only reading that cannot
    silently pick up the host's OS timezone. Callers that mean "the cluster's
    wall clock" — the built-in schedules, ``ava.watcher.cron`` — pass
    ``settings.general.timezone`` explicitly; nothing here reaches for config.

    ``tolerance`` widens the match window for resumable sleep loops. croniter's
    ``get_next`` is strictly greater than the base, so a loop that sleeps until
    the fire minute wakes a few milliseconds past it and the next fire jumps a
    whole period (deterministic miss — Task #958). Passing e.g.
    ``timedelta(minutes=2)`` backs the base up by that much, so a wake within
    the tolerance of the fire time still resolves to the current period's fire
    (the returned instant may then be slightly in the past; callers treat
    ``wait <= threshold`` as "fire now").

    Raises:
        CronExprError: invalid cron expression.
    """
    base = after if after is not None else _dt.datetime.now(_dt.UTC)
    if base.tzinfo is None:
        base = base.replace(tzinfo=_dt.UTC)
    if tolerance is not None:
        base = base - tolerance

    compute_tz = ZoneInfo(timezone) if timezone else _dt.UTC
    base_in_tz = base.astimezone(compute_tz)

    try:
        it = croniter(expr, base_in_tz)
    except (ValueError, KeyError) as exc:
        raise CronExprError(f"invalid cron expression: {expr!r}") from exc
    nxt: _dt.datetime = it.get_next(_dt.datetime)
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=compute_tz)
    return nxt.astimezone(_dt.UTC)


def previous_fire(
    expr: str,
    before: _dt.datetime,
    timezone: str | None = None,
) -> _dt.datetime:
    """Return the cron boundary strictly before ``before`` in UTC.

    ``timezone`` has the same meaning as in :func:`next_fire`: croniter
    interprets the expression in that IANA wall clock, while callers receive
    one timezone-aware UTC instant. A naive ``before`` is interpreted as UTC,
    matching ``next_fire``'s existing compatibility behavior.

    Raises:
        CronExprError: invalid cron expression.
    """
    base = before
    if base.tzinfo is None:
        base = base.replace(tzinfo=_dt.UTC)

    compute_tz = ZoneInfo(timezone) if timezone else _dt.UTC
    base_in_tz = base.astimezone(compute_tz)

    try:
        it = croniter(expr, base_in_tz)
    except (ValueError, KeyError) as exc:
        raise CronExprError(f"invalid cron expression: {expr!r}") from exc
    previous: _dt.datetime = it.get_prev(_dt.datetime)
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=compute_tz)
    return previous.astimezone(_dt.UTC)


# `when` normalization


def normalize_when(when: _dt.datetime | _dt.timedelta | str) -> _dt.datetime:
    """Normalize datetime / timedelta / ISO str to a TZ-aware UTC datetime.

    Naive datetime / ISO string without tz → ValueError.
    """
    if isinstance(when, _dt.datetime):
        if when.tzinfo is None:
            raise ValueError(
                "datetime must carry tzinfo (use datetime.UTC or ZoneInfo); naive datetime is ambiguous"
            )
        return when.astimezone(_dt.UTC)
    if isinstance(when, _dt.timedelta):
        return _dt.datetime.now(_dt.UTC) + when
    if isinstance(when, str):
        parsed = _dt.datetime.fromisoformat(when)
        if parsed.tzinfo is None:
            raise ValueError(
                f"ISO string must include timezone: {when!r} (e.g. '...+00:00' or '...Z')"
            )
        return parsed.astimezone(_dt.UTC)
    raise TypeError(f"when must be datetime / timedelta / str, got {type(when).__name__}")


def normalize_end_time(
    end_time: _dt.datetime | _dt.timedelta | str | None,
) -> _dt.datetime | None:
    """Normalize end_time — same as normalize_when, but allows None."""
    if end_time is None:
        return None
    return normalize_when(end_time)


# Script generation

# Shared preamble for both generated scripts: `_wake(message)` delivers a
# `watcher:N`-tagged chat inbound to the launching agent (identity comes from
# the bootstrap's inlined AVA_AGENT_ID — `_boot.agent_id()` reads it lazily;
# N from the session-id env var the run command sets). Inlined into the
# generated script — the SDK deliberately has no public remind primitive, and
# a generated script may use internal plumbing.
# Must stay free of literal braces: the templates below go through .format().
_WAKE_HELPER = """\
import os as _os

import ava._boot as _boot
from ava import _gateway_client as _gateway_client


def _wake(message):
    _gateway_client.send_message(
        _boot.agent_id(),
        content=message,
        source="watcher:" + _os.environ["AVA_WATCHER_SESSION_ID"],
    )
"""

_AT_TEMPLATE = """\
# Auto-generated time watcher (one-shot). Do not edit manually.
_TEMPLATE_VERSION = {template_version}
import datetime as _dt
import time as _time

{wake_helper}
_WHEN = _dt.datetime.fromisoformat({when_iso!r})
_MESSAGE = {message!r}
{tz_setup}
# Announce the target on stdout (the watcher's session output + log): a
# sleeping watcher is otherwise indistinguishable from a stuck one — the
# session shows only the launch command. One line at startup is enough for a
# one-shot (2026-08-25 false alarm, task #1620). Printed in the cluster's
# timezone (user ruling 2026-08-27: one cluster clock — a runner whose OS
# zone differs must not display a different wall clock), matching the cron
# script's tz-aware display. ASCII only: a C-locale stdout would raise
# UnicodeEncodeError on a non-ASCII character and kill the watcher — the very
# silent death these lines prevent.
print({announce}, flush=True)

while True:
    _delay = (_WHEN - _dt.datetime.now(_dt.UTC)).total_seconds()
    if _delay <= 0:
        break
    _time.sleep(_delay)
    # The wall clock can step during the sleep (laptop resume / NTP
    # correction): sleeping the computed delay does not guarantee the clock
    # reached _WHEN. Re-check against the target instead of firing early
    # (issue #182).
_wake(_MESSAGE)
"""

_CRON_TEMPLATE = """\
# Auto-generated time watcher (recurring cron). Do not edit manually.
_TEMPLATE_VERSION = {template_version}
import datetime as _dt
import time as _time
from zoneinfo import ZoneInfo

from croniter import croniter

{wake_helper}
_EXPR = {expr!r}
_MESSAGE = {message!r}
_TZ = ZoneInfo({timezone!r})
_END = _dt.datetime.fromisoformat({end_time_iso!r}) if {end_time_iso!r} else None


def _next() -> _dt.datetime:
    base = _dt.datetime.now(_dt.UTC).astimezone(_TZ)
    nxt = croniter(_EXPR, base).get_next(_dt.datetime)
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=_TZ)
    return nxt.astimezone(_dt.UTC)


_last = None
while True:
    _fire = _next()
    if _END is not None and _fire > _END:
        # end_time means "no more fires past this point": the next scheduled fire
        # is outside the window, so stop silently. Do NOT wake here — that would
        # emit a duplicate right after the last in-window fire.
        break
    if _last is not None and _fire <= _last:
        # The wall clock stepped backwards: _next() re-resolved the boundary we
        # already fired. Sleep past it (a monotonic sleep, unaffected by clock
        # correction), then re-evaluate — one boundary fires at most once
        # (issue #182).
        _time.sleep((_last - _dt.datetime.now(_dt.UTC)).total_seconds() + 1.0)
        continue
    # Announce the schedule state on stdout (the watcher's session output +
    # log): a healthy watcher sleeping toward its next fire is otherwise
    # indistinguishable from a stuck one — a session capture shows only the
    # launch command. The `next fire` line is the first thing a debugger looks
    # for (2026-08-25 false alarm: a healthy Monday-only cron was misread as a
    # stuck process on a Tuesday — task #1620).
    if _last is None:
        print(
            "[watcher] cron " + _EXPR + " in " + str(_TZ) + " -> next fire at "
            + _fire.astimezone(_TZ).isoformat(),
            flush=True,
        )
    else:
        print(
            "[watcher] fired " + _last.astimezone(_TZ).isoformat()
            + " -> next fire at " + _fire.astimezone(_TZ).isoformat(),
            flush=True,
        )
    while True:
        _delay = (_fire - _dt.datetime.now(_dt.UTC)).total_seconds()
        if _delay <= 0:
            break
        _time.sleep(_delay)
        # The wall clock can step during the sleep (laptop resume / NTP
        # correction): verify the clock actually reached the boundary before
        # firing — a stepped clock must not fire early, then fire again
        # (issue #182).
    _wake(_MESSAGE)
    _last = _fire
"""


def build_at_script(
    *,
    when_iso: str,
    message: str,
    timezone: str | None,
    template_version: int = TEMPLATE_VERSION,
) -> str:
    """Build a one-shot time-watcher script that sleeps until ``when_iso`` (an
    ISO-8601 UTC string) then wakes the launching agent once and exits.

    ``timezone`` (IANA name or None) only drives the startup announcement's
    wall clock — the sleep itself is UTC-based, so a wrong display zone can
    never move the fire time. None renders the announcement in the watcher
    process's own wall clock (the settings-lite degradation: a maintenance
    verb has no authoritative cluster timezone, so the announcement matches
    the wall clock its operator is looking at).
    """
    if timezone is None:
        tz_setup = ""
        announce = '"[watcher] one-shot -> fires at " + _WHEN.astimezone().isoformat()'
    else:
        tz_setup = f"from zoneinfo import ZoneInfo\n_TZ = ZoneInfo({timezone!r})\n"
        announce = '"[watcher] one-shot -> fires at " + _WHEN.astimezone(_TZ).isoformat()'
    return _tw.dedent(_AT_TEMPLATE).format(
        wake_helper=_WAKE_HELPER,
        when_iso=when_iso,
        message=message,
        tz_setup=tz_setup,
        announce=announce,
        template_version=template_version,
    )


def build_cron_script(
    *,
    expr: str,
    message: str,
    timezone: str,
    end_time_iso: str | None,
    template_version: int = TEMPLATE_VERSION,
) -> str:
    """Build a recurring cron-watcher script that wakes the launching agent on
    each cron fire, evaluated in ``timezone``, stopping after ``end_time_iso``
    (ISO-8601 string or None for forever)."""
    return _tw.dedent(_CRON_TEMPLATE).format(
        wake_helper=_WAKE_HELPER,
        expr=expr,
        message=message,
        timezone=timezone,
        end_time_iso=end_time_iso,
        template_version=template_version,
    )
