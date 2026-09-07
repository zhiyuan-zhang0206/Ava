"""Daily rotate-then-retention OS job registration contract."""

from __future__ import annotations

import types
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from shared import os_logs_job as job


@pytest.fixture(autouse=True)
def _configure_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from shared import os_cron

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

    monkeypatch.setattr(job.subprocess, "run", run)

    assert job._register_macos() == 0
    plist = job._launchd_plist_path("ava-deadbeef")
    first = plist.read_text(encoding="utf-8")
    assert job._register_macos() == 0

    assert plist.read_text(encoding="utf-8") == first
    assert [call[1] for call in calls] == ["bootout", "bootstrap", "bootout", "bootstrap"]


def test_legacy_log_retention_job_is_booted_out_and_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = job._legacy_launchd_plist_path("ava-deadbeef")
    legacy.parent.mkdir(parents=True)
    legacy.write_text("<plist/>", encoding="utf-8")
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> types.SimpleNamespace:
        calls.append(argv)
        return _ok()

    monkeypatch.setattr(job.subprocess, "run", run)

    job.reap_legacy_macos_job()

    assert not legacy.exists()
    assert calls == [
        [
            "launchctl",
            "bootout",
            f"gui/{job.os.getuid()}/com.ava.ava-deadbeef.log-retention",
        ]
    ]


def test_legacy_reaper_does_not_touch_os_jobs_when_test_gate_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared import os_cron, platform

    monkeypatch.setattr(os_cron, "os_jobs_enabled", lambda: False)
    monkeypatch.setattr(platform, "IS_MACOS", True)
    monkeypatch.setattr(
        job,
        "reap_legacy_macos_job",
        lambda: pytest.fail("legacy reaper reached launchd with the test gate off"),
    )

    job.reap_legacy_logs_job()


def test_linux_registration_replaces_only_this_clusters_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = "40 4 * * * /other/ava logs rotate  # ava-logs-maintenance.ava-other-cafefeed"
    old = "35 4 * * * /old/ava logs retention  # ava-logs-maintenance.ava-deadbeef"
    written: dict[str, str] = {}

    def which(_name: str) -> str:
        return "/usr/bin/crontab"

    monkeypatch.setattr(job.shutil, "which", which)

    def run(argv: list[str], **kwargs: object) -> types.SimpleNamespace:
        if argv == ["crontab", "-l"]:
            return types.SimpleNamespace(returncode=0, stdout=f"{other}\n{old}\n", stderr="")
        written["body"] = str(kwargs["input"])
        return _ok()

    monkeypatch.setattr(job.subprocess, "run", run)

    assert job._register_linux() == 0
    assert other in written["body"]
    assert old not in written["body"]
    assert written["body"].count("# ava-logs-maintenance.ava-deadbeef") == 1
    assert "40 4 * * *" in written["body"]
    assert "logs rotate" in written["body"] and "logs retention" in written["body"]


def test_converge_registers_then_reaps_legacy_job(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands._converge_os_jobs import ensure_logs_maintenance

    calls: list[str] = []
    monkeypatch.setattr(job, "register_logs_job", lambda: calls.append("register"))
    monkeypatch.setattr(job, "reap_legacy_logs_job", lambda: calls.append("reap"))

    ensure_logs_maintenance(None)  # type: ignore[arg-type]
    assert calls == ["register", "reap"]


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
