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
import re as _re
import textwrap as _tw
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter


class CronExprError(Exception):
    """Invalid cron expression."""


__all__ = [
    "AT_SESSION_TTL_GRACE_SECONDS",
    "DEFAULT_STANDING_CRON_MAX_SECONDS",
    "TEMPLATE_VERSION",
    "CronExprError",
    "build_at_script",
    "build_cron_script",
    "next_fire",
    "normalize_when",
    "previous_fire",
    "session_deadline",
    "validate_cron",
    "validate_timezone",
]


# Standing cron cap (user ruling 2026-09-09, task #2617): a cron registered
# without an explicit end_time lives at most this long, counted from
# registration — `ava.watcher.cron` defaults `end_time` to now + this. A longer
# schedule must pass an explicit end_time. Re-registering the same standing
# schedule renews it (see ava/watcher.py::cron and shared/watcher_registry.
# register_cron_renewal).
DEFAULT_STANDING_CRON_MAX_SECONDS = 7 * 24 * 3600

# The one-shot (`at`) session's hard-deadline grace (user ruling 2026-09-14,
# task #3411): a watcher session's shell TTL IS its target deadline — for an
# at watcher that is `fires_at + this grace`, so the child's wake delivery,
# exit notice, and self-close all finish before the TTL reaper may reclaim
# the session (it also absorbs the reaper's poll cadence). The fire moment
# itself is never extended: an at watcher still wakes at `fires_at` or not
# at all.
AT_SESSION_TTL_GRACE_SECONDS = 300

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
# were multi-generation orphans still firing cron/at); v5 = standing-cron cap
# (the reconcile rebuilds live standing crons so the SDK's now+7d default end
# replaces their NULL end — task #2617); v6 = wake retry (a gateway restart at
# a fire raised out of the bare `_wake` call and killed a live cron watcher —
# 2026-09-15 evidence, task #3525; `_wake` now retries with bounded backoff and
# logs a final failure instead of raising).
TEMPLATE_VERSION = 6


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


# Timeout parsing


_DURATION_RE = _re.compile(r"^(\d+)([smhd])$")


def _parse_timeout(timeout: float | _dt.timedelta | str) -> float:
    """Coerce a timeout to a positive number of seconds.

    Accepts a number of seconds, a `timedelta`, or a `"<n>{s,m,h,d}"` duration
    string (e.g. `"30m"`, `"2h"`). Lives here (not in the SDK) so both the
    SDK's `ava.watcher.launch` and any system-side caller parse one grammar.
    """
    if isinstance(timeout, _dt.timedelta):
        secs = timeout.total_seconds()
    elif isinstance(timeout, bool):  # bool is an int subclass — reject explicitly
        raise TypeError("timeout must be seconds, a timedelta, or a duration string")
    elif isinstance(timeout, (int, float)):
        secs = float(timeout)
    elif isinstance(timeout, str):
        m = _DURATION_RE.match(timeout)
        if not m:
            raise ValueError(
                f"timeout={timeout!r} not recognized — use '<n>s/m/h/d' (e.g. '30m', '2h'), "
                "a number of seconds, or a timedelta"
            )
        n, unit = int(m.group(1)), m.group(2)
        secs = n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    else:
        raise TypeError("timeout must be seconds, a timedelta, or a duration string")
    if secs <= 0:
        raise ValueError("timeout must be positive")
    return secs


# Session deadlines


def session_deadline(
    kind: str,
    *,
    created_at: _dt.datetime | None = None,
    timeout_secs: float | None = None,
    fires_at: _dt.datetime | None = None,
    cron_end_at: _dt.datetime | None = None,
) -> _dt.datetime | None:
    """The moment a watcher's session must be reclaimed — its target deadline.

    One derivation for every surface of the lifecycle (user ruling
    2026-09-14, task #3411: a watcher session's shell TTL IS its target
    deadline, one system — never the registry and the TTL disagreeing). The
    spawn write path (`ava.watcher._spawn`, which folds the remaining TTL on
    every (re)mount), the boot reconcile (rebuild vs reaped), and the
    gateway reaper (reclaim vs heal) all derive through THIS function, so
    they cannot drift apart:

    - ``launch`` — ``created_at + timeout_secs`` (the watchdog horizon; the
      session is created with its watchdog, so created_at is the launch);
    - ``cron`` — ``cron_end_at`` (None = legacy standing row without one);
    - ``at`` — ``fires_at + AT_SESSION_TTL_GRACE_SECONDS``.

    Returns None when the row cannot answer (missing payload — legacy
    rows); callers keep their conservative path.
    """
    if kind == "launch":
        if created_at is None or timeout_secs is None:
            return None
        return created_at + _dt.timedelta(seconds=timeout_secs)
    if kind == "cron":
        return cron_end_at
    if kind == "at":
        if fires_at is None:
            return None
        return fires_at + _dt.timedelta(seconds=AT_SESSION_TTL_GRACE_SECONDS)
    raise ValueError(f"unknown watcher kind {kind!r}")


# Script generation

# Shared preamble for both generated scripts: `_wake(message)` delivers a
# `watcher:N`-tagged chat inbound to the launching agent (identity comes from
# the bootstrap's inlined AVA_AGENT_ID — `_boot.agent_id()` reads it lazily;
# N from the session-id env var the run command sets). Inlined into the
# generated script — the SDK deliberately has no public remind primitive, and
# a generated script may use internal plumbing. Delivery retries 3x with
# bounded backoff on gateway/transport errors and, when every attempt fails,
# logs the failure on stderr and returns False instead of raising — a gateway
# restart must not kill the watcher (task #3525).
# Must stay free of literal braces: the templates below go through .format().
_WAKE_HELPER = """\
import os as _os
import sys as _sys
import time as _time

import ava._boot as _boot
from ava import _gateway_client as _gateway_client

# A wake must survive a gateway restart: until 2026-09-15 the bare
# send_message call raised GatewayUnavailable out of this helper and killed
# the watcher at its fire (a short ConnectError window; task #3525). Three
# attempts with bounded backoff ride out a restart; a final failure is logged
# on stderr (the session log + the wrapper tail) and does not raise, so a cron
# loop keeps its schedule instead of dying silently.
_WAKE_ATTEMPTS = 3
_WAKE_BACKOFF_S = (10.0, 40.0)


def _wake(message):
    for _attempt in range(_WAKE_ATTEMPTS):
        try:
            _gateway_client.send_message(
                _boot.agent_id(),
                content=message,
                source="watcher:" + _os.environ["AVA_WATCHER_SESSION_ID"],
            )
            return True
        except Exception as _exc:
            _last_exc = _exc
            if _attempt + 1 < _WAKE_ATTEMPTS:
                _time.sleep(_WAKE_BACKOFF_S[_attempt])
    print(
        "[watcher] wake delivery failed after "
        + str(_WAKE_ATTEMPTS)
        + " attempts: "
        + repr(_last_exc),
        file=_sys.stderr,
        flush=True,
    )
    return False
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
