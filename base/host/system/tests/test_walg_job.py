"""WAL-G daily tick OS job: the definition it writes, and when it reaches the scheduler."""

from __future__ import annotations

import shlex
import types
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

import pytest

from base.config import settings
from base.host.system import cron
from base.host.system import walg_job as job


@pytest.fixture(autouse=True)
def _configure_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cron, "ava_binary_path", lambda: "/work tree/.venv/bin/ava")
    monkeypatch.setattr(cron, "job_home", lambda: str(tmp_path / ".ava"))
    monkeypatch.setattr(cron, "launchd_path_env", lambda: "/work tree/.venv/bin:/usr/bin")
    monkeypatch.setattr(settings.services, "backup_hour", 3)


def _which_crontab(_name: str) -> str:
    return "/usr/bin/crontab"


def _which_nothing(_name: str) -> None:
    return None


def _ok() -> types.SimpleNamespace:
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def test_the_tick_runs_three_hours_after_the_logical_dump_becomes_due() -> None:
    assert (job._hour(backup_hour_reader=lambda: settings.services.backup_hour), job._MINUTE) == (
        6,
        25,
    )


@pytest.mark.parametrize(("dump_hour", "tick_hour"), [(0, 3), (20, 23), (21, 0), (23, 2)])
def test_the_tick_hour_follows_the_logical_dump_hour_and_wraps(
    monkeypatch: pytest.MonkeyPatch, dump_hour: int, tick_hour: int
) -> None:
    monkeypatch.setattr(settings.services, "backup_hour", dump_hour)

    assert job._hour(backup_hour_reader=lambda: settings.services.backup_hour) == tick_hour


def test_launchd_plist_runs_the_tick_command_daily_and_logs_to_walg_log(tmp_path: Path) -> None:
    content = job._launchd_plist_content(backup_hour_reader=lambda: settings.services.backup_hour)
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


def test_macos_reregistration_rewrites_and_reloads_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> types.SimpleNamespace:
        calls.append(argv)
        return _ok()

    monkeypatch.setattr(cron.subprocess, "run", run)

    assert job._register_macos(backup_hour_reader=lambda: settings.services.backup_hour) == 0
    plist = job._launchd_plist_path()
    first = plist.read_text(encoding="utf-8")
    assert job._register_macos(backup_hour_reader=lambda: settings.services.backup_hour) == 0

    assert plist.read_text(encoding="utf-8") == first
    assert [call[1] for call in calls] == ["bootout", "bootstrap", "bootout", "bootstrap"]


def test_macos_unregister_removes_the_plist_and_is_repeatable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> types.SimpleNamespace:
        calls.append(argv)
        return _ok()

    monkeypatch.setattr(cron.subprocess, "run", run)
    assert job._register_macos(backup_hour_reader=lambda: settings.services.backup_hour) == 0
    plist = job._launchd_plist_path()
    assert plist.exists()

    assert (job._unregister_macos(), job._unregister_macos()) == (0, 0)

    assert not plist.exists()
    assert [call[1] for call in calls][-2:] == ["bootout", "bootout"]


def test_linux_registration_replaces_only_its_own_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    other = "40 4 * * * /x/ava logs rotate  # ava-logs-maintenance"
    old = "25 1 * * * /old/ava backup walg run  # ava-walg"
    written: dict[str, str] = {}

    monkeypatch.setattr(cron.shutil, "which", _which_crontab)

    def run(argv: list[str], **kwargs: object) -> types.SimpleNamespace:
        if argv == ["crontab", "-l"]:
            return types.SimpleNamespace(
                returncode=0, stdout=f"{other}\n\n{old}\n{old}\n", stderr=""
            )
        written["body"] = str(kwargs["input"])
        return _ok()

    monkeypatch.setattr(cron.subprocess, "run", run)

    assert job._register_linux(backup_hour_reader=lambda: settings.services.backup_hour) == 0

    body = written["body"]
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
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cron.shutil, "which", _which_nothing)

    assert job._register_linux(backup_hour_reader=lambda: settings.services.backup_hour) == 1
    assert capsys.readouterr().err == (
        "  * WAL-G backup: crontab not installed; the daily tick cannot be registered\n"
    )


def test_linux_unregister_removes_the_line_and_keeps_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = "40 4 * * * /x/ava logs rotate  # ava-logs-maintenance"
    table = {"text": f"{other}\n25 6 * * * /x/ava backup walg run  # ava-walg\n"}
    monkeypatch.setattr(cron.shutil, "which", _which_crontab)

    def run(argv: list[str], **kwargs: object) -> types.SimpleNamespace:
        if argv == ["crontab", "-l"]:
            return types.SimpleNamespace(returncode=0, stdout=table["text"], stderr="")
        table["text"] = str(kwargs["input"])
        return _ok()

    monkeypatch.setattr(cron.subprocess, "run", run)

    assert job._unregister_linux() == 0

    assert table["text"] == other + "\n"


def test_register_skips_when_os_jobs_are_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    skipped: list[str] = []
    monkeypatch.setattr(cron, "skip_os_job", skipped.append)

    def no_backend() -> None:
        raise AssertionError("backend must not be touched with OS jobs off")

    monkeypatch.setattr("base.host.system.backend.get_backend", no_backend)

    job.register_walg_job(
        enabled_reader=lambda: False, backup_hour_reader=lambda: settings.services.backup_hour
    )

    assert skipped == ["WAL-G tick"]


def test_register_and_unregister_delegate_in_the_default_home(
    default_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def register(*, backup_hour_reader: Callable[[], int]) -> None:
        calls.append("register")

    fake_backend = types.SimpleNamespace(
        register_walg_job=register,
        unregister_walg_job=lambda: calls.append("unregister"),
    )
    monkeypatch.setattr("base.host.system.backend.get_backend", lambda: fake_backend)

    job.register_walg_job(
        enabled_reader=lambda: True, backup_hour_reader=lambda: settings.services.backup_hour
    )
    job.unregister_walg_job()

    assert calls == ["register", "unregister"]
