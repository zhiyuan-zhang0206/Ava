from __future__ import annotations

import ast
import re
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

import pytest

from base.daemon.schedules.watcher import next_fire

_SCHEDULES = Path(__file__).resolve().parents[3] / "schedules"
_CLUSTER_CLOCK_TEMPLATES = {
    "debt-sweep-daily-schedule.py": "TZ",
    "dev-ci-metrics-schedule.py": "TZ",
    "memory-steward-schedule.py": "TIMEZONE",
    "model-update-tracker-schedule.py": "TZ",
    "self-evolution-daily-schedule.py": "TZ",
    "self-evolution-weekly-schedule.py": "TIMEZONE",
}
_CRON_RE = re.compile(r"^[\d*/,\-]+(?: [\d*/,\-]+){4}$")
_IANA = available_timezones()
assert "Asia/Shanghai" in _IANA, "no tz database available; the zone-name check cannot run"


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


def _timezone_arguments(tree: ast.Module) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        found += [ast.unparse(kw.value) for kw in node.keywords if kw.arg == "timezone"]
        callee = ast.unparse(node.func)
        if callee == "ZoneInfo" and node.args:
            found.append(ast.unparse(node.args[0]))
        if callee == "run_daily_loop" and len(node.args) > 2:
            found.append(ast.unparse(node.args[2]))
    return found


@pytest.mark.parametrize(
    "path",
    sorted(_SCHEDULES.glob("*-schedule.py")),
    ids=lambda p: p.name,
)
def test_template_timezone_is_the_cluster_setting(path: Path) -> None:
    tree = _tree(path.name)
    arguments = _timezone_arguments(tree)
    assert arguments, f"{path.name} passes no timezone"
    assert set(arguments) == {"cluster_timezone()"}, arguments
    assert not any(
        isinstance(node, ast.Attribute) and ast.unparse(node) == "settings.general.timezone"
        for node in ast.walk(tree)
    ), f"{path.name} reads settings itself"


@pytest.mark.parametrize("path", sorted(_SCHEDULES.glob("*-schedule.py")), ids=lambda p: p.name)
def test_no_template_hardcodes_an_iana_timezone(path: Path) -> None:
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
    base = datetime(2026, 6, 9, tzinfo=UTC)
    shanghai = next_fire("0 4 * * *", after=base, timezone="Asia/Shanghai")
    pacific = next_fire("0 4 * * *", after=base, timezone="America/Los_Angeles")
    assert shanghai == datetime(2026, 6, 9, 20, 0, tzinfo=UTC)
    assert pacific == datetime(2026, 6, 9, 11, 0, tzinfo=UTC)


def test_weekly_cron_lands_on_the_cluster_weekday() -> None:
    base = datetime(2026, 6, 9, tzinfo=UTC)
    fire = next_fire("0 0 * * 2", after=base, timezone="Asia/Shanghai")
    assert fire.astimezone(ZoneInfo("Asia/Shanghai")).weekday() == 1
    assert fire.astimezone(ZoneInfo("America/Los_Angeles")).weekday() == 0
