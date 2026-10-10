"""Live job policy readers preserve gate order and platform generation timing."""

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from base.host.system import autostart, backend, cron, logs_job, packages_job, pr_flow_job, walg_job


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
    )
    assert reads == (["enabled", "home"] if enabled else ["enabled"])


def test_packages_refresh_gate_follows_home_and_precedes_backend(
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
    monkeypatch.setattr(backend, "get_backend", lambda: pytest.fail("backend reached"))
    packages_job.register_packages_job(
        enabled_reader=lambda: True,
        refresh_enabled_reader=refresh_reader,
        tick_reader=_unexpected_read,
    )
    assert reads == ["home", "refresh"]


@pytest.mark.parametrize("platform", [backend.MacPlatformBackend, backend.LinuxPlatformBackend])
@pytest.mark.parametrize("kind", ["packages", "walg"])
def test_real_backend_keeps_reader_live_at_each_definition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: type[backend.PlatformBackend],
    kind: str,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cron, "job_home", lambda: str(tmp_path / ".ava"))
    monkeypatch.setattr(cron, "ava_binary_path", lambda: "/isolated/.venv/bin/ava")
    monkeypatch.setattr(cron, "launchd_path_env", lambda: "/usr/bin")

    def owns(_kind: str) -> bool:
        return True

    monkeypatch.setattr(cron, "owns_os_jobs", owns)

    def which(_name: str) -> str:
        return "/usr/bin/crontab"

    monkeypatch.setattr(cron.shutil, "which", which)
    tables: list[str] = []
    operations: list[str] = []

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        operations.append(argv[1])
        if argv == ["crontab", "-"]:
            tables.append(str(kwargs["input"]))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cron.subprocess, "run", run)
    monkeypatch.setattr(backend, "get_backend", platform)
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
            )
        else:
            walg_job.register_walg_job(
                enabled_reader=lambda: True, backup_hour_reader=reader(owner)
            )
        if platform is backend.LinuxPlatformBackend:
            return tables[-1]
        label = "packages-refresh" if kind == "packages" else "walg"
        return (tmp_path / "Library" / "LaunchAgents" / f"com.ava.{label}.plist").read_text()

    first = register("first")
    second = register("second")
    state["first"] = state["second"]
    updated = register("first")
    assert first != second
    assert updated == second
    expected_reads = 2 if platform is backend.MacPlatformBackend else 1
    assert (
        reads
        == ["first"] * expected_reads + ["second"] * expected_reads + ["first"] * expected_reads
    )
    assert operations == (
        ["bootout", "bootstrap"] * 3 if platform is backend.MacPlatformBackend else ["-l", "-"] * 3
    )
    if kind == "packages":
        assert (
            ("*/20 " in first and "*/30 " in second)
            if platform is backend.LinuxPlatformBackend
            else ("<integer>1200</integer>" in first and "<integer>1800</integer>" in second)
        )
    else:
        assert (
            ("25 0 " in first and "25 2 " in second)
            if platform is backend.LinuxPlatformBackend
            else ("<integer>0</integer>" in first and "<integer>2</integer>" in second)
        )


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
    register(enabled_reader=disabled)
    assert reads == ["enabled"]
