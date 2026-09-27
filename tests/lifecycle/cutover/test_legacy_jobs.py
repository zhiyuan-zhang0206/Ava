"""Exact-slug attribution and retirement of legacy OS jobs (fake scheduler only)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import cutover_legacy_jobs as jobs
from shared.cluster import home_slug
from tests.lifecycle.cutover.conftest import LegacyHome

Make = Callable[..., LegacyHome]


def test_exact_slug_ignores_a_sibling_whose_slug_extends_ours(make_legacy: Make) -> None:
    legacy = make_legacy()
    sibling = home_slug(legacy.sibling)
    assert sibling.startswith(legacy.slug)  # the prefix trap is real in this fixture
    found = jobs.discover(legacy.home, legacy.scheduler.host())
    labels = {job.label for job in found.launchd}
    assert labels == {
        f"com.ava.{legacy.slug}.watchdog-probe.agent-runner",
        f"com.ava.{legacy.slug}.hold-watchdog",
        f"com.ava.{legacy.slug}.logs-maintenance",
        f"com.ava.{legacy.slug}.packages-refresh",
        f"com.ava.{legacy.slug}.autostart",
        f"com.ava.permissions-helper.{legacy.slug}",
    }
    assert all(sibling not in line.line for line in found.cron)
    assert [line.kind for line in found.cron] == [
        "health-probe",
        "watchdog-probe",
        "hold-watchdog",
        "autostart",
    ]
    assert found.ambiguous == ()


def test_unmarked_lines_belong_only_to_the_exact_home(tmp_path: Path) -> None:
    home = tmp_path / ".ava"
    content = "\n".join(
        [
            f"*/5 * * * * AVA_HOME={home} /x/ava cluster health-probe",
            f"*/5 * * * * AVA_HOME={home}-other /x/ava cluster health-probe",
            "*/5 * * * * /x/ava cluster hold-watchdog",
            "0 * * * * /usr/local/bin/memory-pull.sh",
        ]
    )
    ours, ambiguous = jobs.classify_crontab(content, home, home_slug(home))
    assert [(line.kind, line.marked) for line in ours] == [("health-probe", False)]
    assert ambiguous == ["*/5 * * * * /x/ava cluster hold-watchdog"]


def test_our_marker_naming_another_home_is_ambiguous(tmp_path: Path) -> None:
    home = tmp_path / ".ava"
    slug = home_slug(home)
    line = f"* * * * * AVA_HOME=/elsewhere /x/ava cluster hold-watchdog  # ava-hold-watchdog.{slug}"
    ours, ambiguous = jobs.classify_crontab(line, home, slug)
    assert ours == [] and ambiguous == [line]


def test_launchd_retirement_boots_out_then_archives_the_plist(make_legacy: Make) -> None:
    legacy = make_legacy()
    host = legacy.scheduler.host()
    job = next(
        job for job in jobs.discover(legacy.home, host).launchd if job.kind == "hold-watchdog"
    )
    archive = legacy.home / "cutover-rollback" / "os-jobs"
    jobs.retire_launchd(host, job, archive)
    jobs.retire_launchd(host, job, archive)  # idempotent: not loaded, plist already moved
    assert job.label not in legacy.scheduler.loaded()
    assert not Path(job.plist).exists() and (archive / Path(job.plist).name).exists()


def test_bootout_failure_of_a_still_loaded_job_raises(make_legacy: Make, tmp_path: Path) -> None:
    legacy = make_legacy()
    host = legacy.scheduler.host()
    job = next(job for job in jobs.discover(legacy.home, host).launchd if job.loaded)
    broken = tmp_path / "launchctl-broken"
    broken.write_text(
        f'#!/bin/sh\nif [ "$1" = list ]; then exec {host.binary("launchctl")} list; fi\nexit 5\n'
    )
    broken.chmod(0o755)
    commands = tuple(
        (name, str(broken) if name == "launchctl" else path) for name, path in host.commands
    )
    with pytest.raises(RuntimeError, match="bootout"):
        jobs.retire_launchd(replace(host, commands=commands), job, tmp_path)
    assert Path(job.plist).exists()


def test_linux_units_retire_exactly_through_their_managers(make_legacy: Make) -> None:
    legacy = make_legacy(roles=("gateway", "agent-runner"), platform="linux")
    host = legacy.scheduler.host()
    found = jobs.discover(legacy.home, host)
    assert found.launchd == ()
    assert {(unit.kind, unit.scope) for unit in found.units} == {
        ("autostart", "system"),
        ("gate", "user"),
        ("lgtm", "user"),
    }
    archive = legacy.home / "cutover-rollback" / "os-jobs"
    for unit in found.units:
        jobs.retire_unit(host, unit, archive)
    root = legacy.scheduler.root
    assert sorted(path.name for path in (root / "user-units").iterdir()) == [
        f"com.ava.loki.{home_slug(legacy.sibling)}.service"
    ]
    assert sorted(path.name for path in (root / "system-units").iterdir()) == [
        f"ava-boot.{home_slug(legacy.sibling)}.service"
    ]
    assert (archive / f"ava-boot.{legacy.slug}.service").read_text().startswith("Description=")
    calls = legacy.scheduler.calls()
    assert f"sudo -n systemctl disable --now ava-boot.{legacy.slug}.service" in calls
    assert f"systemctl --user disable --now com.ava.gate.{legacy.slug}.service" in calls


def test_cron_removal_keeps_every_other_line_and_a_preimage(make_legacy: Make) -> None:
    legacy = make_legacy()
    host = legacy.scheduler.host()
    before = legacy.scheduler.crontab()
    ours = tuple(line.line for line in jobs.discover(legacy.home, host).cron)
    archive = legacy.home / "cutover-rollback" / "os-jobs"
    assert jobs.remove_cron_lines(host, ours, archive) == 4
    assert jobs.remove_cron_lines(host, ours, archive) == 0
    assert legacy.scheduler.crontab() == [line for line in before if line not in ours]
    assert (archive / "crontab.before").read_text().splitlines() == before
