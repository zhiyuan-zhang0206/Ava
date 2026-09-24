"""Recurring content-refresh OS job registration contract."""

from __future__ import annotations

import types
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from shared import os_cron
from shared import os_packages as job


@pytest.fixture(autouse=True)
def _configure_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(os_cron, "_home_slug", lambda: "ava-deadbeef")
    monkeypatch.setattr(os_cron, "ava_binary_path", lambda: "/work tree/.venv/bin/ava")
    monkeypatch.setattr(os_cron, "job_home", lambda: str(tmp_path / ".ava"))
    monkeypatch.setattr(os_cron, "launchd_path_env", lambda: "/work tree/.venv/bin:/usr/bin")


def _ok() -> types.SimpleNamespace:
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def test_launchd_plist_runs_the_refresh_pass_on_the_tick(tmp_path: Path) -> None:
    from shared.config import settings

    content = job._launchd_plist_content()
    root = ET.fromstring(content)  # noqa: S314 — self-generated plist
    values = [element.text for element in root.findall("./dict/array/string")]
    command = values[2]

    assert "com.ava.ava-deadbeef.packages-refresh" in content
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

    monkeypatch.setattr(os_cron.subprocess, "run", run)

    assert job._register_macos() == 0
    plist = job._launchd_plist_path("ava-deadbeef")
    first = plist.read_text(encoding="utf-8")
    assert job._register_macos() == 0

    assert plist.read_text(encoding="utf-8") == first
    assert [call[1] for call in calls] == ["bootout", "bootstrap", "bootout", "bootstrap"]


def test_linux_registration_replaces_only_this_clusters_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    other = "*/15 * * * * /other/ava packages refresh --from-job  # ava-packages-refresh.ava-other-cafefeed"
    old = "*/15 * * * * /old/ava packages refresh --from-job  # ava-packages-refresh.ava-deadbeef"
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
    assert written["body"].count("# ava-packages-refresh.ava-deadbeef") == 1
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

    monkeypatch.setattr(os_cron.shutil, "which", missing)
    assert job._register_linux() == 1
    assert (
        "packages refresh: crontab not installed; the recurring refresh pass cannot be registered"
        in capsys.readouterr().err
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
    assert "skipping packages-refresh registration to avoid clobbering" in capsys.readouterr().err

    read.stderr = "no crontab for user"
    assert job._register_linux() == 0
    assert (
        len(writes),
        writes[0].startswith("*/15 * * * * "),
        writes[0].endswith("# ava-packages-refresh.ava-deadbeef\n"),
        writes[0].count("\n"),
    ) == (1, True, True, 1)

    read.returncode = 0
    assert (
        job._unregister_linux("ava-deadbeef"),
        writes[-1],
        job._unregister_linux("ava-deadbeef"),
        len(writes),
    ) == (0, "\n", 0, 2)

    read.stdout = writes[0]
    write_failure = True
    assert job._register_linux() == 1
    assert "crontab update failed: write denied" in capsys.readouterr().err
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


def test_windows_registration_uses_a_minute_task(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []

    def create(kind: str, args: tuple[str, ...], minutes: int, *, time_limit_s: int) -> None:
        calls.append((kind, args, minutes, time_limit_s))

    monkeypatch.setattr("shared.os_schtasks.create_minute_task", create)

    assert job._register_windows() is None
    assert calls == [("packages-refresh", ("packages", "refresh", "--from-job"), 15, 900)]


def test_windows_registration_reports_a_failure_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    def create(*_a: object, **_kw: object) -> str:
        return "denied"

    monkeypatch.setattr("shared.os_schtasks.create_minute_task", create)
    assert job._register_windows() == "denied"


def test_register_is_gated_by_os_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    skipped: list[str] = []
    monkeypatch.setattr(os_cron, "os_jobs_enabled", lambda: False)
    monkeypatch.setattr(os_cron, "skip_os_job", skipped.append)
    monkeypatch.setattr(
        "shared.platform_backend.get_backend",
        lambda: pytest.fail("registration reached the backend with the gate off"),
    )

    job.register_packages_job()
    assert skipped == ["packages refresh"]


def test_register_is_skipped_when_refresh_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.config import settings

    monkeypatch.setattr(os_cron, "os_jobs_enabled", lambda: True)
    monkeypatch.setattr(settings.packages, "refresh_enabled", False)
    monkeypatch.setattr(
        "shared.platform_backend.get_backend",
        lambda: pytest.fail("registration reached the backend with refresh disabled"),
    )

    job.register_packages_job()


def test_register_dispatches_to_the_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.config import settings

    calls: list[str] = []

    class Backend:
        def register_packages_job(self) -> None:
            calls.append("register")

    monkeypatch.setattr(os_cron, "os_jobs_enabled", lambda: True)
    monkeypatch.setattr(settings.packages, "refresh_enabled", True)
    monkeypatch.setattr("shared.platform_backend.get_backend", Backend)

    job.register_packages_job()
    assert calls == ["register"]


def test_unregister_delegates_with_the_home_slug(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from shared.cluster import slug_for_home

    calls: list[str] = []

    class Backend:
        def unregister_packages_job(self, slug: str) -> None:
            calls.append(slug)

    monkeypatch.setattr("shared.platform_backend.get_backend", Backend)

    home = tmp_path / ".ava-target"
    job.unregister_packages_job(home)
    assert calls == [slug_for_home(home)]


def test_converge_registers_the_refresh_job(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands._converge_os_jobs import ensure_packages_refresh_job

    calls: list[str] = []
    monkeypatch.setattr(job, "register_packages_job", lambda: calls.append("register"))

    ensure_packages_refresh_job(None)  # type: ignore[arg-type]
    assert calls == ["register"]
