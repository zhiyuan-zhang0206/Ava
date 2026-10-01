"""Recurring content-refresh OS job registration contract."""

from __future__ import annotations

import types
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from base.host.system import cron
from base.host.system import packages_job as job


@pytest.fixture(autouse=True)
def _configure_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cron, "ava_binary_path", lambda: "/work tree/.venv/bin/ava")
    monkeypatch.setattr(cron, "job_home", lambda: str(tmp_path / ".ava"))
    monkeypatch.setattr(cron, "launchd_path_env", lambda: "/work tree/.venv/bin:/usr/bin")


def _ok() -> types.SimpleNamespace:
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def test_launchd_plist_runs_the_refresh_pass_on_the_tick(tmp_path: Path) -> None:
    from base.config import settings

    content = job._launchd_plist_content()
    root = ET.fromstring(content)  # noqa: S314 — self-generated plist
    values = [element.text for element in root.findall("./dict/array/string")]
    command = values[2]

    assert "<string>com.ava.packages-refresh</string>" in content
    assert values[0:2] == ["/bin/sh", "-c"]
    assert command is not None
    assert "'/work tree/.venv/bin/ava' packages refresh --from-job" in command
    tick = settings.packages.refresh_tick_seconds
    assert f"<key>StartInterval</key>\n    <integer>{tick}</integer>" in content
    assert "<key>RunAtLoad</key>\n    <false/>" in content
    assert f"{tmp_path}/.ava/logs/packages-refresh.log" in content


def test_macos_reregistration_rewrites_and_reloads_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> types.SimpleNamespace:
        calls.append(argv)
        return _ok()

    monkeypatch.setattr(cron.subprocess, "run", run)

    assert job._register_macos() == 0
    plist = job._launchd_plist_path()
    first = plist.read_text(encoding="utf-8")
    assert job._register_macos() == 0

    assert plist.read_text(encoding="utf-8") == first
    assert [call[1] for call in calls] == ["bootout", "bootstrap", "bootout", "bootstrap"]


def test_linux_registration_replaces_only_its_own_lines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    other = "40 4 * * * /x/ava logs rotate  # ava-logs-maintenance"
    old = "*/15 * * * * /old/ava packages refresh --from-job  # ava-packages-refresh"
    written: dict[str, str] = {}

    def which(_name: str) -> str:
        return "/usr/bin/crontab"

    monkeypatch.setattr(cron.shutil, "which", which)

    def run(argv: list[str], **kwargs: object) -> types.SimpleNamespace:
        if argv == ["crontab", "-l"]:
            return types.SimpleNamespace(
                returncode=0, stdout=f"{other}\n\n{old}\n{old}\n", stderr=""
            )
        written["body"] = str(kwargs["input"])
        return _ok()

    monkeypatch.setattr(cron.subprocess, "run", run)

    assert job._register_linux() == 0
    assert other in written["body"]
    assert f"{other}\n\n" in written["body"]
    assert old not in written["body"]
    assert written["body"].count("# ava-packages-refresh") == 1
    assert "*/15 * * * *" in written["body"]
    assert "packages refresh --from-job" in written["body"]
    assert str(tmp_path / ".ava" / "logs" / "packages-refresh.log") in written["body"]


def test_linux_crontab_failures_and_empty_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(_name: str) -> None:
        return None

    def available(_name: str) -> str:
        return "/usr/bin/crontab"

    monkeypatch.setattr(cron.shutil, "which", missing)
    assert job._register_linux() == 1
    assert capsys.readouterr().err == (
        "  * packages refresh: crontab not installed; the recurring refresh "
        "pass cannot be registered\n"
    )

    monkeypatch.setattr(cron.shutil, "which", available)
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

    monkeypatch.setattr(cron.subprocess, "run", run)
    assert job._register_linux() == 1
    assert writes == []
    assert capsys.readouterr().err == (
        "  * crontab -l failed (permission denied); "
        "skipping packages-refresh registration to avoid clobbering the crontab\n"
    )

    read.stderr = "no crontab for user"
    assert job._register_linux() == 0
    assert (
        len(writes),
        writes[0].startswith("*/15 * * * * "),
        writes[0].endswith("  # ava-packages-refresh\n"),
        writes[0].count("\n"),
    ) == (1, True, True, 1)

    read.returncode = 0
    assert (
        job._unregister_linux(),
        writes[-1],
        job._unregister_linux(),
        len(writes),
    ) == (0, "\n", 0, 2)

    read.stdout = writes[0]
    write_failure = True
    assert job._register_linux() == 1
    assert capsys.readouterr().err == "  * crontab update failed: write denied\n"
    assert job._unregister_linux() == 1
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

    monkeypatch.setattr(cron.subprocess, "run", run)
    monkeypatch.setattr(job.logger, "error", record_error)
    assert job._register_macos() == 1
    assert [call[1] for call in calls] == ["bootout", "bootstrap"]
    assert errors == [
        ("launchctl bootstrap failed for {}: {}", "com.ava.packages-refresh", "denied")
    ]

    plist = job._launchd_plist_path()
    assert plist.exists()
    assert job._unregister_macos() == 0
    assert job._unregister_macos() == 0
    assert not plist.exists()
    assert [call[1] for call in calls] == ["bootout", "bootstrap", "bootout", "bootout"]


def test_register_is_gated_by_os_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    skipped: list[str] = []
    monkeypatch.setattr(cron, "os_jobs_enabled", lambda: False)
    monkeypatch.setattr(cron, "skip_os_job", skipped.append)
    monkeypatch.setattr(
        "base.host.system.backend.get_backend",
        lambda: pytest.fail("registration reached the backend with the gate off"),
    )

    job.register_packages_job()
    assert skipped == ["packages refresh"]


def test_register_is_skipped_when_refresh_disabled(
    default_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from base.config import settings

    monkeypatch.setattr(cron, "os_jobs_enabled", lambda: True)
    monkeypatch.setattr(settings.packages, "refresh_enabled", False)
    monkeypatch.setattr(
        "base.host.system.backend.get_backend",
        lambda: pytest.fail("registration reached the backend with refresh disabled"),
    )

    job.register_packages_job()


def test_register_dispatches_to_the_backend(
    default_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from base.config import settings

    calls: list[str] = []

    class Backend:
        def register_packages_job(self) -> None:
            calls.append("register")

    monkeypatch.setattr(cron, "os_jobs_enabled", lambda: True)
    monkeypatch.setattr(settings.packages, "refresh_enabled", True)
    monkeypatch.setattr("base.host.system.backend.get_backend", Backend)

    job.register_packages_job()
    assert calls == ["register"]


def test_unregister_delegates_to_the_backend(
    default_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    class Backend:
        def unregister_packages_job(self) -> None:
            calls.append("unregister")

    monkeypatch.setattr("base.host.system.backend.get_backend", Backend)

    job.unregister_packages_job()
    assert calls == ["unregister"]
