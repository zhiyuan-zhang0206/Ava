"""The built-in schedule templates fire on the CLUSTER wall clock.

`schedules/*.py` are deployment templates, not importable modules (each ends in
a `while True:` loop), so the lock reads their source: every timezone they pass
to `next_fire` must be `settings.general.timezone`, and none may name an IANA
zone of its own. That is the invariant, not the cosmetics — three built-ins
hard-coded `Asia/Shanghai` while `blob_vacuum` hard-coded
`America/Los_Angeles`, so one fleet held three incompatible answers to "when is
this cluster idle" and a US-west host ran its 04:00 maintenance at 13:00 local,
mid-peak. The weekly triggers were worse than late: `0 0 * * 2` read on the
wrong clock lands on the wrong calendar DAY.

The behavioural half then walks each template's real cron literals and asserts
the fire instant lands on that cron's hour in the cluster's timezone.
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

import pytest

from shared.watcher import next_fire

_SCHEDULES = Path(__file__).resolve().parents[2] / "schedules"
# Templates whose cron expressions are wall-clock-sensitive. trace-ship-tempo is
# excluded on purpose: `*/5 * * * *` is a period, not a time of day, and it says
# so by passing "UTC" explicitly.
_CLUSTER_CLOCK_TEMPLATES = {
    "memory-steward-schedule.py": "TIMEZONE",
    "self-evolution-daily-schedule.py": "TZ",
    "self-evolution-weekly-schedule.py": "TIMEZONE",
}
_CRON_RE = re.compile(r"^[\d*/,\-]+(?: [\d*/,\-]+){4}$")
_IANA = available_timezones()
# An empty zone database would make the hard-coding check pass by matching
# nothing — a silent no-op is worse than a failure, so refuse to run that way.
assert "Asia/Shanghai" in _IANA, "no tz database available; the zone-name check cannot run"


def _module_constant(tree: ast.Module, name: str) -> ast.expr:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return node.value
    raise AssertionError(f"no module-level {name} assignment")


def _cron_literals(tree: ast.Module) -> list[str]:
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _CRON_RE.match(node.value)
    ]


def _tree(filename: str) -> ast.Module:
    return ast.parse((_SCHEDULES / filename).read_text(encoding="utf-8"))


@pytest.mark.parametrize(("filename", "constant"), sorted(_CLUSTER_CLOCK_TEMPLATES.items()))
def test_template_timezone_is_the_cluster_setting(filename: str, constant: str) -> None:
    """The timezone constant is read from config, never spelled out in the file."""
    assert ast.unparse(_module_constant(_tree(filename), constant)) == "settings.general.timezone"


@pytest.mark.parametrize("path", sorted(_SCHEDULES.glob("*-schedule.py")), ids=lambda p: p.name)
def test_no_template_hardcodes_an_iana_timezone(path: Path) -> None:
    """A literal zone name in a template is the regression: it silently pins one
    deployment's clock onto every cluster that ever runs the built-in."""
    literals = [
        node.value
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    named = [v for v in literals if "/" in v and v in _IANA]
    assert named == [], f"{path.name} hard-codes {named}"


@pytest.mark.parametrize("tz", ["Asia/Shanghai", "America/Los_Angeles", "Asia/Kathmandu"])
@pytest.mark.parametrize("filename", sorted(_CLUSTER_CLOCK_TEMPLATES))
def test_template_crons_fire_on_cluster_wall_clock(filename: str, tz: str) -> None:
    """Given a cluster timezone, each template's cron fires at its stated hour in
    THAT timezone — the same UTC instant across the fleet, and a different one
    per cluster. Asia/Kathmandu (+05:45) is in the list because a half/quarter
    offset catches an implementation that rounds to whole hours.
    """
    tree = _tree(filename)
    crons = _cron_literals(tree)
    assert crons, f"{filename} carries no cron literal"
    base = datetime(2026, 6, 9, tzinfo=UTC)
    for expr in crons:
        minute, hour = expr.split()[:2]
        fire = next_fire(expr, after=base, timezone=tz)
        local = fire.astimezone(ZoneInfo(tz))
        assert (local.hour, local.minute) == (int(hour), int(minute)), f"{filename}: {expr}"


def test_same_cron_yields_different_instants_per_cluster_timezone() -> None:
    """The point of the setting: 04:00 in Shanghai and 04:00 in Los Angeles are
    not the same moment, and the schedule must follow the cluster, not the host."""
    base = datetime(2026, 6, 9, tzinfo=UTC)
    shanghai = next_fire("0 4 * * *", after=base, timezone="Asia/Shanghai")
    pacific = next_fire("0 4 * * *", after=base, timezone="America/Los_Angeles")
    assert shanghai == datetime(2026, 6, 9, 20, 0, tzinfo=UTC)
    assert pacific == datetime(2026, 6, 9, 11, 0, tzinfo=UTC)


def test_weekly_cron_lands_on_the_cluster_weekday() -> None:
    """`0 0 * * 2` is Tuesday on the CLUSTER's calendar. Read on a host nine
    hours west it fired on Monday — a whole day off, not an hour."""
    base = datetime(2026, 6, 9, tzinfo=UTC)
    fire = next_fire("0 0 * * 2", after=base, timezone="Asia/Shanghai")
    assert fire.astimezone(ZoneInfo("Asia/Shanghai")).weekday() == 1  # Tuesday
    assert fire.astimezone(ZoneInfo("America/Los_Angeles")).weekday() == 0  # Monday there
