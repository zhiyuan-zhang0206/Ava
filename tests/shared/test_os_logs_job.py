"""Daily rotate-then-retention OS job registration contract."""

from __future__ import annotations

import types
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from shared import os_cron
from shared import os_logs_job as job


@pytest.fixture(autouse=True)
def _configure_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(os_cron, "_home_slug", lambda: "ava-deadbeef")
    monkeypatch.setattr(os_cron, "ava_binary_path", lambda: "/work tree/.venv/bin/ava")
    monkeypatch.setattr(os_cron, "job_home", lambda: "/home/u/.ava")
    monkeypatch.setattr(os_cron, "launchd_path_env", lambda: "/work tree/.venv/bin:/usr/bin")


def _ok() -> types.SimpleNamespace:
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def test_launchd_plist_runs_rotate_then_retention_daily() -> None:
    content = job._launchd_plist_content()
    root = ET.fromstring(content)  # noqa: S314 — self-generated plist
    values = [element.text for element in root.findall("./dict/array/string")]
    command = values[2]

    assert "com.ava.ava-deadbeef.logs-maintenance" in content
    assert values[0:2] == ["/bin/sh", "-c"]
    assert command is not None
    assert "'/work tree/.venv/bin/ava' logs rotate" in command
    assert "&& '/work tree/.venv/bin/ava' logs retention" in command
    assert f"--family-days {job.FAMILY_DAYS}" in command
    assert "<key>Hour</key>\n            <integer>4</integer>" in content
    assert "<key>Minute</key>\n            <integer>40</integer>" in content
    assert "<key>RunAtLoad</key>\n    <false/>" in content
    assert "/home/u/.ava/logs/logs-maintenance.out.log" in content


def test_macos_reregistration_rewrites_and_reloads_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> types.SimpleNamespace:
        calls.append(argv)
        return _ok()

    monkeypatch.setattr(os_cron.subprocess, "run", run)

    assert job._register_macos() == 0
    plist = job._launchd_plist_path("ava-deadbeef")
    first = plist.read_text(encoding="utf-8")
    assert job._register_macos() == 0

    assert plist.read_text(encoding="utf-8") == first
    assert [call[1] for call in calls] == ["bootout", "bootstrap", "bootout", "bootstrap"]


def test_macos_unregister_removes_only_the_requested_clusters_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = job._launchd_plist_path("ava-deadbeef")
    target.parent.mkdir(parents=True)
    target.write_text("<target-plist/>", encoding="utf-8")
    other = job._launchd_plist_path("ava-other-cafefeed")
    other.write_text("<other-plist/>", encoding="utf-8")
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> types.SimpleNamespace:
        calls.append(argv)
        return _ok()

    monkeypatch.setattr(os_cron.subprocess, "run", run)

    assert job._unregister_macos("ava-deadbeef") == 0

    assert not target.exists()
    assert other.read_bytes() == b"<other-plist/>"
    assert calls == [
        [
            "launchctl",
            "bootout",
            f"gui/{os_cron.os.getuid()}/com.ava.ava-deadbeef.logs-maintenance",
        ]
    ]


def test_linux_registration_replaces_only_this_clusters_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = "40 4 * * * /other/ava logs rotate  # ava-logs-maintenance.ava-other-cafefeed"
    old = "35 4 * * * /old/ava logs retention  # ava-logs-maintenance.ava-deadbeef"
    written: dict[str, str] = {}

    def which(_name: str) -> str:
        return "/usr/bin/crontab"

    monkeypatch.setattr(os_cron.shutil, "which", which)

    def run(argv: list[str], **kwargs: object) -> types.SimpleNamespace:
        if argv == ["crontab", "-l"]:
            return types.SimpleNamespace(
                returncode=0, stdout=f"{other}\n\n{old}\n{old}\n", stderr=""
            )
        written["body"] = str(kwargs["input"])
        return _ok()

    monkeypatch.setattr(os_cron.subprocess, "run", run)

    assert job._register_linux() == 0
    assert other in written["body"]
    assert f"{other}\n\n" in written["body"]
    assert old not in written["body"]
    assert written["body"].count("# ava-logs-maintenance.ava-deadbeef") == 1
    assert "40 4 * * *" in written["body"]
    assert "logs rotate" in written["body"] and "logs retention" in written["body"]


def test_linux_crontab_failures_and_empty_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(_name: str) -> None:
        return None

    def available(_name: str) -> str:
        return "/usr/bin/crontab"

    monkeypatch.setattr(os_cron.shutil, "which", missing)
    assert job._register_linux() == 1
    assert capsys.readouterr().err == (
        "  * logs maintenance: crontab not installed; daily rotation and "
        "retention cannot be registered\n"
    )

    monkeypatch.setattr(os_cron.shutil, "which", available)
    writes: list[str] = []
    read = types.SimpleNamespace(returncode=1, stdout="stale", stderr="permission denied")
    write_failure = False

    def run(argv: list[str], **kwargs: object) -> types.SimpleNamespace:
        if argv == ["crontab", "-l"]:
            return read
        if write_failure:
            return types.SimpleNamespace(returncode=1, stderr="write denied")
        writes.append(str(kwargs["input"]))
        read.stdout = str(kwargs["input"])
        return _ok()

    monkeypatch.setattr(os_cron.subprocess, "run", run)
    assert job._register_linux() == 1
    assert writes == []
    assert capsys.readouterr().err == (
        "  * crontab -l failed (permission denied); "
        "skipping logs-maintenance registration to avoid clobbering the crontab\n"
    )

    read.stderr = "no crontab for user"
    assert job._register_linux() == 0
    assert (
        len(writes),
        writes[0].startswith("40 4 * * * "),
        writes[0].endswith("# ava-logs-maintenance.ava-deadbeef\n"),
        writes[0].count("\n"),
    ) == (1, True, True, 1)

    read.returncode = 0
    read.stdout = writes[0]
    assert (
        job._unregister_linux("ava-deadbeef"),
        writes[-1],
        job._unregister_linux("ava-deadbeef"),
        len(writes),
    ) == (0, "\n", 0, 2)

    read.stdout = writes[0]
    write_failure = True
    assert job._register_linux() == 1
    assert capsys.readouterr().err == "  * crontab update failed: write denied\n"
    assert job._unregister_linux("ava-deadbeef") == 1
    assert len(writes) == 2


def test_macos_bootstrap_failure_and_repeated_unregister(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    errors: list[tuple[object, ...]] = []

    def run(argv: list[str], **_kwargs: object) -> types.SimpleNamespace:
        calls.append(argv)
        return types.SimpleNamespace(returncode=1 if argv[1] == "bootstrap" else 0, stderr="denied")

    def record_error(*args: object) -> None:
        errors.append(args)

    monkeypatch.setattr(os_cron.subprocess, "run", run)
    monkeypatch.setattr(job.logger, "error", record_error)
    assert job._register_macos() == 1
    assert [call[1] for call in calls] == ["bootout", "bootstrap"]
    assert errors == [
        ("launchctl bootstrap failed for {}: {}", job._label("ava-deadbeef"), "denied")
    ]

    plist = job._launchd_plist_path("ava-deadbeef")
    assert plist.exists()
    assert job._unregister_macos("ava-deadbeef") == 0
    assert job._unregister_macos("ava-deadbeef") == 0
    assert not plist.exists()
    assert [call[1] for call in calls] == ["bootout", "bootstrap", "bootout", "bootout"]


def test_converge_registers_logs_maintenance(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands._converge_os_jobs import ensure_logs_maintenance

    calls: list[str] = []
    monkeypatch.setattr(job, "register_logs_job", lambda: calls.append("register"))

    ensure_logs_maintenance(None)  # type: ignore[arg-type]
    assert calls == ["register"]


def test_windows_registration_uses_two_daily_tasks_one_minute_apart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared import os_schtasks

    calls: list[tuple[str, tuple[str, ...], int, int]] = []

    def create(
        kind: str,
        args: tuple[str, ...],
        *,
        hour: int,
        minute: int,
        time_limit_s: int,
    ) -> None:
        assert time_limit_s == 1800
        calls.append((kind, args, hour, minute))

    monkeypatch.setattr(os_schtasks, "create_daily_task", create)

    assert job._register_windows() is None
    assert calls == [
        ("logs-rotate", ("logs", "rotate"), 4, 40),
        (
            "logs-retention",
            ("logs", "retention", "--family-days", job.FAMILY_DAYS),
            4,
            41,
        ),
    ]
