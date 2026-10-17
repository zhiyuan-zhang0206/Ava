"""Daily rotate-then-retention OS job registration contract."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from base.host.system import cron
from base.host.system import logs_job as job
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


def _meiva(*, host: Host) -> str:
    return "/work tree/.venv/bin/meiva"


def _path(*, host: Host) -> str:
    return "/work tree/.venv/bin:/usr/bin"


def _launchctl(**replies: Reply) -> tuple[FakeCommands, Host]:
    """A macOS host whose launchctl verbs answer as given (default: success)."""
    commands = FakeCommands()
    for verb, reply in replies.items():
        commands.on(["launchctl", verb], reply, repeat=True)
    commands.on("launchctl", Reply(), repeat=True)
    return commands, fake_host(commands, platform=DARWIN_ARM64, uid=_UID)


def _linux(crontab: FakeCrontab) -> Host:
    return fake_host(crontab, platform=LINUX_X86, programs={"crontab": "/usr/bin/crontab"})


@pytest.fixture(autouse=True)
def _configure_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cron, "meiva_binary_path", _meiva)
    monkeypatch.setattr(cron, "job_home", lambda: "/home/u/.meiva")
    monkeypatch.setattr(cron, "launchd_path_env", _path)


def test_launchd_plist_runs_rotate_then_retention_daily() -> None:
    content = job.launchd_plist_content(host=fake_host(platform=DARWIN_ARM64))
    root = ET.fromstring(content)  # noqa: S314 — self-generated plist
    values = [element.text for element in root.findall("./dict/array/string")]
    command = values[2]

    assert "<string>com.meiva.logs-maintenance</string>" in content
    assert values[0:2] == ["/bin/sh", "-c"]
    assert command is not None
    assert "'/work tree/.venv/bin/meiva' logs rotate" in command
    assert "&& '/work tree/.venv/bin/meiva' logs retention" in command
    assert f"--family-days {job.FAMILY_DAYS}" in command
    assert "<key>Hour</key>\n            <integer>4</integer>" in content
    assert "<key>Minute</key>\n            <integer>40</integer>" in content
    assert "<key>RunAtLoad</key>\n    <false/>" in content
    assert "/home/u/.meiva/logs/logs-maintenance.out.log" in content


def test_macos_reregistration_rewrites_and_reloads_idempotently() -> None:
    commands, host = _launchctl()

    assert job.register_macos(host=host) == 0
    plist = job.launchd_plist_path()
    first = plist.read_text(encoding="utf-8")
    assert job.register_macos(host=host) == 0

    assert plist.read_text(encoding="utf-8") == first
    assert [argv[1] for argv in commands.argvs] == ["bootout", "bootstrap", "bootout", "bootstrap"]


def test_macos_unregister_removes_only_its_own_job() -> None:
    target = job.launchd_plist_path()
    target.parent.mkdir(parents=True)
    target.write_text("<target-plist/>", encoding="utf-8")
    other = target.parent / "com.meiva.packages-refresh.plist"
    other.write_text("<other-plist/>", encoding="utf-8")
    commands, host = _launchctl()

    assert job.unregister_macos(host=host) == 0

    assert not target.exists()
    assert other.read_bytes() == b"<other-plist/>"
    assert commands.argvs == [("launchctl", "bootout", f"gui/{_UID}/com.meiva.logs-maintenance")]


def test_linux_registration_replaces_only_its_own_lines() -> None:
    other = "*/15 * * * * /x/meiva packages refresh --from-job  # meiva-packages-refresh"
    old = "35 4 * * * /old/meiva logs retention  # meiva-logs-maintenance"
    crontab = FakeCrontab(f"{other}\n\n{old}\n{old}\n")

    assert job.register_linux(host=_linux(crontab)) == 0
    body = crontab.writes[-1]
    assert other in body
    assert f"{other}\n\n" in body
    assert old not in body
    assert body.count("# meiva-logs-maintenance") == 1
    assert "40 4 * * *" in body
    assert "logs rotate" in body and "logs retention" in body


def test_linux_crontab_failures_and_empty_table(capsys: pytest.CaptureFixture[str]) -> None:
    assert job.register_linux(host=fake_host(platform=LINUX_X86)) == 1
    assert capsys.readouterr().err == (
        "  * logs maintenance: crontab not installed; daily rotation and "
        "retention cannot be registered\n"
    )

    crontab = FakeCrontab("stale")
    crontab.read_returncode = 1
    crontab.read_stderr = "permission denied"
    host = _linux(crontab)
    assert job.register_linux(host=host) == 1
    assert crontab.writes == []
    assert capsys.readouterr().err == (
        "  * crontab -l failed (permission denied); "
        "skipping logs-maintenance registration to avoid clobbering the crontab\n"
    )

    crontab.read_stderr = "no crontab for user"
    assert job.register_linux(host=host) == 0
    assert (
        len(crontab.writes),
        crontab.writes[0].startswith("40 4 * * * "),
        crontab.writes[0].endswith("  # meiva-logs-maintenance\n"),
        crontab.writes[0].count("\n"),
    ) == (1, True, True, 1)

    crontab.read_returncode = 0
    assert (
        job.unregister_linux(host=host),
        crontab.writes[-1],
        job.unregister_linux(host=host),
        len(crontab.writes),
    ) == (0, "\n", 0, 2)

    crontab.table = crontab.writes[0]
    crontab.write_failure = "write denied"
    assert job.register_linux(host=host) == 1
    assert capsys.readouterr().err == "  * crontab update failed: write denied\n"
    assert job.unregister_linux(host=host) == 1
    assert len(crontab.writes) == 2


def test_macos_bootstrap_failure_and_repeated_unregister(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors: list[tuple[object, ...]] = []

    def record_error(*args: object) -> None:
        errors.append(args)

    commands, host = _launchctl(bootstrap=Reply(stderr="denied", returncode=1))
    monkeypatch.setattr(job.logger, "error", record_error)
    assert job.register_macos(host=host) == 1
    assert [argv[1] for argv in commands.argvs] == ["bootout", "bootstrap"]
    assert errors == [
        ("launchctl bootstrap failed for {}: {}", "com.meiva.logs-maintenance", "denied")
    ]

    plist = job.launchd_plist_path()
    assert plist.exists()
    assert job.unregister_macos(host=host) == 0
    assert job.unregister_macos(host=host) == 0
    assert not plist.exists()
    assert [argv[1] for argv in commands.argvs] == ["bootout", "bootstrap", "bootout", "bootout"]
