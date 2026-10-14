"""Live job policy readers preserve gate order and platform generation timing."""

from collections.abc import Callable
from pathlib import Path

import pytest

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


def _isolated_ava(*, host: Host) -> str:
    return "/isolated/.venv/bin/ava"


def _usr_bin(*, host: Host) -> str:
    return "/usr/bin"


def _refuse(message: str) -> Callable[..., None]:
    def refuse(*_args: object, **_kwargs: object) -> None:
        pytest.fail(message)

    return refuse


def _unexpected_read() -> int:
    pytest.fail("an early return read scheduling policy")


@pytest.mark.parametrize("enabled", [False, True])
def test_packages_gates_do_not_read_tick(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    reads: list[str] = []

    def enabled_reader() -> bool:
        reads.append("enabled")
        return enabled

    def owns(_kind: str) -> bool:
        reads.append("home")
        return False

    monkeypatch.setattr(cron, "owns_os_jobs", owns)
    packages_job.register_packages_job(
        enabled_reader=enabled_reader,
        refresh_enabled_reader=lambda: bool(_unexpected_read()),
        tick_reader=_unexpected_read,
        host=fake_host(),
    )
    assert reads == (["enabled", "home"] if enabled else ["enabled"])


def test_packages_refresh_gate_follows_home_and_precedes_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[str] = []

    def owns(_kind: str) -> bool:
        reads.append("home")
        return True

    def refresh_reader() -> bool:
        reads.append("refresh")
        return False

    monkeypatch.setattr(cron, "owns_os_jobs", owns)
    monkeypatch.setattr(cron, "register_platform_job", _refuse("platform registrar reached"))
    packages_job.register_packages_job(
        enabled_reader=lambda: True,
        refresh_enabled_reader=refresh_reader,
        tick_reader=_unexpected_read,
        host=fake_host(),
    )
    assert reads == ["home", "refresh"]


def _isolate_jobs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the generated job specs at a scratch home, and make that home the default one."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cron, "job_home", lambda: str(tmp_path / ".ava"))
    monkeypatch.setattr(cron, "ava_binary_path", _isolated_ava)
    monkeypatch.setattr(cron, "launchd_path_env", _usr_bin)

    def owns(_kind: str) -> bool:
        return True

    monkeypatch.setattr(cron, "owns_os_jobs", owns)


def _assert_definitions(kind: str, platform: str, first: str, second: str) -> None:
    """Each registration carries the value its own reader returned at definition time."""
    if kind == "packages":
        assert (
            ("*/20 " in first and "*/30 " in second)
            if platform == "linux"
            else ("<integer>1200</integer>" in first and "<integer>1800</integer>" in second)
        )
    else:
        assert (
            ("25 0 " in first and "25 2 " in second)
            if platform == "linux"
            else ("<integer>0</integer>" in first and "<integer>2</integer>" in second)
        )


@pytest.mark.parametrize("platform", ["mac", "linux"])
@pytest.mark.parametrize("kind", ["packages", "walg"])
def test_real_platform_job_keeps_reader_live_at_each_definition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
    kind: str,
) -> None:
    _isolate_jobs(monkeypatch, tmp_path)
    crontab = FakeCrontab()
    launchctl = FakeCommands()
    launchctl.on("launchctl", Reply(), repeat=True)
    runner: CommandRunner = crontab if platform == "linux" else launchctl
    host = fake_host(
        runner,
        platform=LINUX_X86 if platform == "linux" else DARWIN_ARM64,
        programs={"crontab": "/usr/bin/crontab"},
    )
    state = {
        "first": 1200 if kind == "packages" else 21,
        "second": 1800 if kind == "packages" else 23,
    }
    reads: list[str] = []

    def reader(owner: str) -> Callable[[], int]:
        def read() -> int:
            reads.append(owner)
            return state[owner]

        return read

    def register(owner: str) -> str:
        if kind == "packages":
            packages_job.register_packages_job(
                enabled_reader=lambda: True,
                refresh_enabled_reader=lambda: True,
                tick_reader=reader(owner),
                host=host,
            )
        else:
            walg_job.register_walg_job(
                enabled_reader=lambda: True, backup_hour_reader=reader(owner), host=host
            )
        if platform == "linux":
            return crontab.writes[-1]
        label = "packages-refresh" if kind == "packages" else "walg"
        return (tmp_path / "Library" / "LaunchAgents" / f"com.ava.{label}.plist").read_text()

    first = register("first")
    second = register("second")
    state["first"] = state["second"]
    updated = register("first")
    assert first != second
    assert updated == second
    expected_reads = 2 if platform == "mac" else 1
    assert (
        reads
        == ["first"] * expected_reads + ["second"] * expected_reads + ["first"] * expected_reads
    )
    operations = [argv[1] for argv in (crontab.argvs if platform == "linux" else launchctl.argvs)]
    assert operations == (["bootout", "bootstrap"] * 3 if platform == "mac" else ["-l", "-"] * 3)
    _assert_definitions(kind, platform, first, second)


@pytest.mark.parametrize(
    "register",
    [
        autostart.register_autostart,
        cron.register_os_cron,
        logs_job.register_logs_job,
        pr_flow_job.register_pr_flow_job,
    ],
)
def test_every_registrar_reads_gate_once_before_home(
    monkeypatch: pytest.MonkeyPatch,
    register: Callable[..., None],
) -> None:
    reads: list[str] = []

    def disabled() -> bool:
        reads.append("enabled")
        return False

    def owns(_kind: str) -> bool:
        pytest.fail("home reached")

    monkeypatch.setattr(cron, "owns_os_jobs", owns)
    if register is pr_flow_job.register_pr_flow_job:
        register(enabled_reader=disabled, production=True, host=fake_host())
    else:
        register(enabled_reader=disabled, host=fake_host())
    assert reads == ["enabled"]
