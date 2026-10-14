"""WAL-G daily tick OS job: the definition it writes, and when it reaches the scheduler."""

from __future__ import annotations

import shlex
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

import pytest

from base.config import settings
from base.host.system import cron
from base.host.system import walg_job as job
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import (
    DARWIN_ARM64,
    LINUX_X86,
    FakeCommands,
    Reply,
    fake_host,
)
from base.host.system.tests.fake_crontab import FakeCrontab

_UID = 501


def _ava(*, host: Host) -> str:
    return "/work tree/.venv/bin/ava"


def _path(*, host: Host) -> str:
    return "/work tree/.venv/bin:/usr/bin"


def _launchctl() -> tuple[FakeCommands, Host]:
    """A macOS host whose launchctl always succeeds, and the commands it was asked."""
    commands = FakeCommands()
    commands.on("launchctl", Reply(), repeat=True)
    return commands, fake_host(commands, platform=DARWIN_ARM64, uid=_UID)


def _linux(crontab: FakeCrontab) -> Host:
    return fake_host(crontab, platform=LINUX_X86, programs={"crontab": "/usr/bin/crontab"})


def _recorder(calls: list[str], label: str) -> Callable[..., None]:
    def record(*_args: object, **_kwargs: object) -> None:
        calls.append(label)

    return record


@pytest.fixture(autouse=True)
def _configure_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cron, "ava_binary_path", _ava)
    monkeypatch.setattr(cron, "job_home", lambda: str(tmp_path / ".ava"))
    monkeypatch.setattr(cron, "launchd_path_env", _path)
    monkeypatch.setattr(settings.services, "backup_hour", 3)


def test_the_tick_runs_three_hours_after_the_logical_dump_becomes_due() -> None:
    assert (job.hour(backup_hour_reader=lambda: settings.services.backup_hour), job.MINUTE) == (
        6,
        25,
    )


@pytest.mark.parametrize(("dump_hour", "tick_hour"), [(0, 3), (20, 23), (21, 0), (23, 2)])
def test_the_tick_hour_follows_the_logical_dump_hour_and_wraps(
    monkeypatch: pytest.MonkeyPatch, dump_hour: int, tick_hour: int
) -> None:
    monkeypatch.setattr(settings.services, "backup_hour", dump_hour)

    assert job.hour(backup_hour_reader=lambda: settings.services.backup_hour) == tick_hour


def test_launchd_plist_runs_the_tick_command_daily_and_logs_to_walg_log(tmp_path: Path) -> None:
    content = job.launchd_plist_content(
        backup_hour_reader=lambda: settings.services.backup_hour,
        host=fake_host(platform=DARWIN_ARM64),
    )
    root = ET.fromstring(content)  # noqa: S314 — self-generated plist
    values = [element.text for element in root.findall("./dict/array/string")]

    assert "<string>com.ava.walg</string>" in content
    assert values == ["/bin/sh", "-c", "'/work tree/.venv/bin/ava' backup walg run"]
    assert (
        "<key>StartCalendarInterval</key>\n    <dict>\n"
        "            <key>Hour</key>\n            <integer>6</integer>\n"
        "            <key>Minute</key>\n            <integer>25</integer>\n"
    ) in content
    assert "<key>RunAtLoad</key>\n    <false/>" in content
    assert content.count(f"{tmp_path}/.ava/logs/walg.log") == 2
    assert f"<string>{tmp_path}/.ava</string>" in content  # AVA_HOME pin


def test_macos_reregistration_rewrites_and_reloads_idempotently() -> None:
    commands, host = _launchctl()

    assert (
        job.register_macos(backup_hour_reader=lambda: settings.services.backup_hour, host=host) == 0
    )
    plist = job.launchd_plist_path()
    first = plist.read_text(encoding="utf-8")
    assert (
        job.register_macos(backup_hour_reader=lambda: settings.services.backup_hour, host=host) == 0
    )

    assert plist.read_text(encoding="utf-8") == first
    assert [argv[1] for argv in commands.argvs] == ["bootout", "bootstrap", "bootout", "bootstrap"]


def test_macos_unregister_removes_the_plist_and_is_repeatable() -> None:
    commands, host = _launchctl()
    assert (
        job.register_macos(backup_hour_reader=lambda: settings.services.backup_hour, host=host) == 0
    )
    plist = job.launchd_plist_path()
    assert plist.exists()

    assert (job.unregister_macos(host=host), job.unregister_macos(host=host)) == (0, 0)

    assert not plist.exists()
    assert [argv[1] for argv in commands.argvs][-2:] == ["bootout", "bootout"]


def test_linux_registration_replaces_only_its_own_line(tmp_path: Path) -> None:
    other = "40 4 * * * /x/ava logs rotate  # ava-logs-maintenance"
    old = "25 1 * * * /old/ava backup walg run  # ava-walg"
    crontab = FakeCrontab(f"{other}\n\n{old}\n{old}\n")

    assert (
        job.register_linux(
            backup_hour_reader=lambda: settings.services.backup_hour, host=_linux(crontab)
        )
        == 0
    )

    body = crontab.writes[-1]
    assert f"{other}\n\n" in body
    assert old not in body
    assert body.count("# ava-walg") == 1
    (line,) = [line for line in body.splitlines() if "# ava-walg" in line]
    assert line.startswith("25 6 * * * ")
    # `/bin/sh -c <quoted command>`: the shell word after -c is the command itself.
    command = shlex.split(line.split(" /bin/sh -c ", 1)[1])[0]
    assert command == "'/work tree/.venv/bin/ava' backup walg run"
    assert f">> {tmp_path}/.ava/logs/walg.log 2>&1" in line
    assert line.endswith("  # ava-walg")


def test_linux_without_crontab_reports_and_registers_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        job.register_linux(
            backup_hour_reader=lambda: settings.services.backup_hour,
            host=fake_host(platform=LINUX_X86),
        )
        == 1
    )
    assert capsys.readouterr().err == (
        "  * WAL-G backup: crontab not installed; the daily tick cannot be registered\n"
    )


def test_linux_unregister_removes_the_line_and_keeps_the_rest() -> None:
    other = "40 4 * * * /x/ava logs rotate  # ava-logs-maintenance"
    crontab = FakeCrontab(f"{other}\n25 6 * * * /x/ava backup walg run  # ava-walg\n")

    assert job.unregister_linux(host=_linux(crontab)) == 0

    assert crontab.table == other + "\n"


def test_register_skips_when_os_jobs_are_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    skipped: list[str] = []
    monkeypatch.setattr(cron, "skip_os_job", skipped.append)

    def no_platform(*_a: object, **_k: object) -> None:
        raise AssertionError("the platform must not be touched with OS jobs off")

    monkeypatch.setattr(cron, "register_platform_job", no_platform)

    job.register_walg_job(
        enabled_reader=lambda: False,
        backup_hour_reader=lambda: settings.services.backup_hour,
        host=fake_host(),
    )

    assert skipped == ["WAL-G tick"]


def test_register_and_unregister_delegate_in_the_default_home(
    default_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(cron, "register_platform_job", _recorder(calls, "register"))
    monkeypatch.setattr(cron, "unregister_platform_job", _recorder(calls, "unregister"))

    job.register_walg_job(
        enabled_reader=lambda: True,
        backup_hour_reader=lambda: settings.services.backup_hour,
        host=fake_host(),
    )
    job.unregister_walg_job(host=fake_host())

    assert calls == ["register", "unregister"]
