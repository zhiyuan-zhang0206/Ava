"""Formal converge roots share live configuration without early policy reads."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.clock.tests.manual_timers import ManualTimers
from base.cluster.derive import production_identity
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.host.proc import CommandRunner
from base.host.system import autostart, cron, logs_job, packages_job, pr_flow_job, walg_job
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import (
    DARWIN_ARM64,
    LINUX_X86,
    FakeCommands,
    Reply,
    fake_host,
)
from base.host.system.tests.fake_crontab import FakeCrontab
from base.sessions.tests.refuse_helper import refuse_helper
from base.telemetry import EventPipeline
from cli.commands.converge.os_jobs import (
    ensure_cluster_autostart,
    ensure_health_probe_cron,
    ensure_logs_maintenance,
    ensure_packages_refresh_job,
    ensure_pr_flow_job,
    ensure_walg_job,
)
from cli.commands.converge.spec import ConvergeCtx, ConvergeStep
from cli.commands.lifecycle import converge_host as host
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def _isolated_ava(*, host: Host) -> str:
    return "/isolated/.venv/bin/ava"


def _usr_bin(*, host: Host) -> str:
    return "/usr/bin"


def test_converge_constructs_one_lazy_owner_per_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
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
            command=command_runtime,
        )
    assert observed[0] is observed[1]
    assert observed[2] is observed[3]
    assert observed[0].config is observed[2].config is command_runtime.config
    assert observed[0].machine is observed[2].machine is command_runtime.machine


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
        machine=machine_from_boot(owner),
        helper=refuse_helper,
        host=fake_host(),
        services=frozenset(),
        timers=ManualTimers(),
    )
    readers: list[Callable[[], bool]] = []
    ticks: list[Callable[[], int]] = []
    hours: list[Callable[[], int]] = []
    refresh: list[Callable[[], bool]] = []
    removed: list[bool] = []

    def register(*, enabled_reader: Callable[[], bool], host: Host) -> None:
        readers.append(enabled_reader)

    def register_packages(
        *,
        enabled_reader: Callable[[], bool],
        refresh_enabled_reader: Callable[[], bool],
        tick_reader: Callable[[], int],
        host: Host,
    ) -> None:
        readers.append(enabled_reader)
        refresh.append(refresh_enabled_reader)
        ticks.append(tick_reader)

    def register_walg(
        *, enabled_reader: Callable[[], bool], backup_hour_reader: Callable[[], int], host: Host
    ) -> None:
        readers.append(enabled_reader)
        hours.append(backup_hour_reader)

    monkeypatch.setattr(cron, "register_os_cron", register)
    monkeypatch.setattr(autostart, "register_autostart", register)
    monkeypatch.setattr(logs_job, "register_logs_job", register)

    def register_pr_flow(
        *, enabled_reader: Callable[[], bool], production: bool, host: Host
    ) -> None:
        assert production is production_identity(machine=ctx.machine, home=ctx.ava_home)
        register(enabled_reader=enabled_reader, host=host)

    monkeypatch.setattr(pr_flow_job, "register_pr_flow_job", register_pr_flow)
    monkeypatch.setattr(packages_job, "register_packages_job", register_packages)
    monkeypatch.setattr(walg_job, "register_walg_job", register_walg)

    def fake_unregister_walg_job(*, host: Host) -> None:
        removed.append(True)

    monkeypatch.setattr(walg_job, "unregister_walg_job", fake_unregister_walg_job)
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
    owner = ConfigBoot()
    for name, value in [
        ("os_jobs_enabled", True),
        ("refresh_enabled", True),
        ("refresh_tick_seconds", 1200),
        ("backup_hour", 21),
        ("walg_config_file", default_home / "walg.json"),
    ]:
        owner.set_field(name, value)
    crontab = FakeCrontab()
    launchctl = FakeCommands()
    launchctl.on("launchctl", Reply(), repeat=True)
    runner: CommandRunner = launchctl if platform == "macos" else crontab
    ctx = ConvergeCtx(
        repo=default_home,
        ava_home=default_home,
        roles=None,
        config=owner,
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=machine_from_boot(owner),
        helper=refuse_helper,
        host=fake_host(
            runner,
            platform=DARWIN_ARM64 if platform == "macos" else LINUX_X86,
            programs={"crontab": "/usr/bin/crontab"},
        ),
        services=frozenset(),
        timers=ManualTimers(),
    )
    monkeypatch.setattr(cron, "ava_binary_path", _isolated_ava)
    monkeypatch.setattr(cron, "launchd_path_env", _usr_bin)

    def specs() -> tuple[str, str]:
        ensure_packages_refresh_job(ctx)
        ensure_walg_job(ctx)
        if platform == "linux":
            line = {
                marker: next(ln for ln in crontab.lines if ln.endswith(marker))
                for marker in ("# ava-packages-refresh", "# ava-walg")
            }
            return line["# ava-packages-refresh"], line["# ava-walg"]
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
