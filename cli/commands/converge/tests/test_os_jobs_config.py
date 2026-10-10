"""Formal converge roots share live configuration without early policy reads."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.config import ConfigBoot
from base.host.system import autostart, cron, logs_job, packages_job, pr_flow_job, walg_job
from base.telemetry import EventPipeline
from cli.commands.converge import host
from cli.commands.converge._os_jobs import (
    ensure_cluster_autostart,
    ensure_health_probe_cron,
    ensure_logs_maintenance,
    ensure_packages_refresh_job,
    ensure_pr_flow_job,
    ensure_walg_job,
)
from cli.commands.converge.spec import ConvergeCtx, ConvergeStep
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def test_converge_constructs_one_lazy_owner_per_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    observed: list[ConvergeCtx] = []

    def unexpected_prepare(_owner: ConfigBoot) -> None:
        pytest.fail("context construction delivered configuration")

    monkeypatch.setattr(ConfigBoot, "prepare", unexpected_prepare)
    steps = (ConvergeStep("first", observed.append), ConvergeStep("second", observed.append))
    for _ in range(2):
        host.converge_host(
            tmp_path,
            None,
            ava_home=tmp_path,
            services=frozenset(),
            steps=steps,
            database_factory=operator_database,
            producer=operator_pipeline,
        )
    assert observed[0] is observed[1]
    assert observed[2] is observed[3]
    assert observed[0].config is not observed[2].config
    assert not observed[0].config.prepared and not observed[2].config.prepared


def test_all_six_formal_steps_pass_live_readers_from_the_same_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    owner = ConfigBoot()
    for name, value in [
        ("os_jobs_enabled", True),
        ("refresh_enabled", True),
        ("refresh_tick_seconds", 1200),
        ("backup_hour", 21),
        ("walg_config_file", tmp_path / "walg.json"),
    ]:
        owner.set_field(name, value)
    ctx = ConvergeCtx(
        repo=tmp_path,
        ava_home=tmp_path,
        roles=None,
        config=owner,
        database_factory=operator_database,
        producer=operator_pipeline,
    )
    readers: list[Callable[[], bool]] = []
    ticks: list[Callable[[], int]] = []
    hours: list[Callable[[], int]] = []
    refresh: list[Callable[[], bool]] = []
    removed: list[bool] = []

    def register(*, enabled_reader: Callable[[], bool]) -> None:
        readers.append(enabled_reader)

    def register_packages(
        *,
        enabled_reader: Callable[[], bool],
        refresh_enabled_reader: Callable[[], bool],
        tick_reader: Callable[[], int],
    ) -> None:
        readers.append(enabled_reader)
        refresh.append(refresh_enabled_reader)
        ticks.append(tick_reader)

    def register_walg(
        *, enabled_reader: Callable[[], bool], backup_hour_reader: Callable[[], int]
    ) -> None:
        readers.append(enabled_reader)
        hours.append(backup_hour_reader)

    monkeypatch.setattr(cron, "register_os_cron", register)
    monkeypatch.setattr(autostart, "register_autostart", register)
    monkeypatch.setattr(logs_job, "register_logs_job", register)
    monkeypatch.setattr(pr_flow_job, "register_pr_flow_job", register)
    monkeypatch.setattr(packages_job, "register_packages_job", register_packages)
    monkeypatch.setattr(walg_job, "register_walg_job", register_walg)
    monkeypatch.setattr(walg_job, "unregister_walg_job", lambda: removed.append(True))
    for step in [
        ensure_health_probe_cron,
        ensure_cluster_autostart,
        ensure_logs_maintenance,
        ensure_packages_refresh_job,
        ensure_pr_flow_job,
        ensure_walg_job,
    ]:
        step(ctx)
    assert len(readers) == 6
    assert [read() for read in readers] == [True] * 6
    assert (refresh[0](), ticks[0](), hours[0]()) == (True, 1200, 21)
    owner.set_field("os_jobs_enabled", False)
    owner.set_field("refresh_enabled", False)
    owner.set_field("refresh_tick_seconds", 1800)
    owner.set_field("backup_hour", 23)
    assert [read() for read in readers] == [False] * 6
    assert (refresh[0](), ticks[0](), hours[0]()) == (False, 1800, 23)
    owner.set_field("walg_config_file", None)
    ensure_walg_job(ctx)
    assert removed == [True]
    assert len(readers) == 6


@pytest.mark.parametrize("platform", ["macos", "linux"])
def test_formal_steps_reach_real_backend_with_live_policy_and_isolated_os(
    monkeypatch: pytest.MonkeyPatch,
    default_home: Path,
    platform: str,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    from types import SimpleNamespace

    from base.host.system import backend

    owner = ConfigBoot()
    for name, value in [
        ("os_jobs_enabled", True),
        ("refresh_enabled", True),
        ("refresh_tick_seconds", 1200),
        ("backup_hour", 21),
        ("walg_config_file", default_home / "walg.json"),
    ]:
        owner.set_field(name, value)
    ctx = ConvergeCtx(
        repo=default_home,
        ava_home=default_home,
        roles=None,
        config=owner,
        database_factory=operator_database,
        producer=operator_pipeline,
    )
    monkeypatch.setattr(
        backend,
        "get_backend",
        backend.MacPlatformBackend if platform == "macos" else backend.LinuxPlatformBackend,
    )
    monkeypatch.setattr(cron, "ava_binary_path", lambda: "/isolated/.venv/bin/ava")
    monkeypatch.setattr(cron, "launchd_path_env", lambda: "/usr/bin")
    tables: list[str] = []

    def which(_name: str) -> str:
        return "/usr/bin/crontab"

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        assert argv[0] in {"launchctl", "crontab"}
        if argv == ["crontab", "-"]:
            tables.append(str(kwargs["input"]))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cron.shutil, "which", which)
    monkeypatch.setattr(cron.subprocess, "run", run)

    def specs() -> tuple[str, str]:
        ensure_packages_refresh_job(ctx)
        ensure_walg_job(ctx)
        if platform == "linux":
            return tables[-2], tables[-1]
        directory = Path.home() / "Library" / "LaunchAgents"
        return (
            (directory / "com.ava.packages-refresh.plist").read_text(),
            (directory / "com.ava.walg.plist").read_text(),
        )

    initial = specs()
    owner.set_field("refresh_tick_seconds", 1800)
    owner.set_field("backup_hour", 23)
    changed = specs()
    assert initial != changed
    if platform == "linux":
        assert initial[0].startswith("*/20 ") and changed[0].startswith("*/30 ")
        assert initial[1].startswith("25 0 ") and changed[1].startswith("25 2 ")
    else:
        assert "<integer>1200</integer>" in initial[0]
        assert "<integer>1800</integer>" in changed[0]
        assert "<integer>0</integer>" in initial[1]
        assert "<integer>2</integer>" in changed[1]
