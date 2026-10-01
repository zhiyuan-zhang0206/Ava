"""The OS-job gates, and what a generated job spec is anchored to.

`AVA_OS_JOBS_ENABLED` stops the suite from arming a job at all; the default-home
gate (`owns_os_jobs`) stops any other home from registering or removing one,
because labels and crontab markers name a job, not a home; and the anchoring makes
any job that IS armed name the binary and `$AVA_HOME` of the checkout that wrote
it — so no stale job can resolve onto the prod install.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from base.config import settings
from base.host.system import autostart, cron, logs_job, packages_job, pr_flow_job, walg_job


class _ExplodingBackend:
    """A backend whose every registration path is a test failure."""

    def register_cron(self, **_kw: object) -> None:
        raise AssertionError("register_cron reached the OS with the gate off")

    def register_autostart(self) -> None:
        raise AssertionError("register_autostart reached the OS with the gate off")

    def register_logs_job(self) -> None:
        raise AssertionError("register_logs_job reached the OS with the gate off")

    def register_packages_job(self) -> None:
        raise AssertionError("register_packages_job reached the OS with the gate off")

    def register_pr_flow_job(self) -> None:
        raise AssertionError("register_pr_flow_job reached the OS with the gate off")

    def register_walg_job(self) -> None:
        raise AssertionError("register_walg_job reached the OS with the gate off")

    def unregister_cron(self) -> None:
        raise AssertionError("unregister_cron reached the OS outside the default home")

    def unregister_autostart(self) -> None:
        raise AssertionError("unregister_autostart reached the OS outside the default home")

    def unregister_logs_job(self) -> None:
        raise AssertionError("unregister_logs_job reached the OS outside the default home")

    def unregister_packages_job(self) -> None:
        raise AssertionError("unregister_packages_job reached the OS outside the default home")

    def unregister_pr_flow_job(self) -> None:
        raise AssertionError("unregister_pr_flow_job reached the OS outside the default home")

    def unregister_walg_job(self) -> None:
        raise AssertionError("unregister_walg_job reached the OS outside the default home")


class _RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def register_cron(self, **_kw: object) -> None:
        self.calls.append("cron")

    def register_autostart(self) -> None:
        self.calls.append("autostart")

    def register_logs_job(self) -> None:
        self.calls.append("logs-maintenance")

    def register_packages_job(self) -> None:
        self.calls.append("packages-refresh")

    def register_pr_flow_job(self) -> None:
        self.calls.append("pr-flow")

    def register_walg_job(self) -> None:
        self.calls.append("walg")

    def unregister_cron(self) -> None:
        self.calls.append("unregister-cron")

    def unregister_autostart(self) -> None:
        self.calls.append("unregister-autostart")

    def unregister_logs_job(self) -> None:
        self.calls.append("unregister-logs-maintenance")

    def unregister_packages_job(self) -> None:
        self.calls.append("unregister-packages-refresh")

    def unregister_pr_flow_job(self) -> None:
        self.calls.append("unregister-pr-flow")

    def unregister_walg_job(self) -> None:
        self.calls.append("unregister-walg")


@pytest.fixture()
def backend(monkeypatch: pytest.MonkeyPatch) -> _RecordingBackend:
    rec = _RecordingBackend()
    monkeypatch.setattr("base.host.system.backend.get_backend", lambda: rec)
    return rec


@pytest.fixture()
def gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite pins the gate OFF for every test (tests/fixtures/env_bootstrap.py); the few
    cases that assert the ENABLED behaviour turn it back on for themselves."""
    monkeypatch.setattr(settings.general, "os_jobs_enabled", True)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_suite_default_is_off() -> None:
    """The whole suite runs with registration disabled — this is the invariant the
    e2e leak violated (nine launchd health probes on a dev box), so assert it
    directly rather than trusting the conftest comment."""
    assert cron.os_jobs_enabled() is False


_REGISTERS = [
    pytest.param(cron.register_os_cron, id="health-probe"),
    pytest.param(autostart.register_autostart, id="autostart"),
    pytest.param(logs_job.register_logs_job, id="logs-maintenance"),
    pytest.param(packages_job.register_packages_job, id="packages-refresh"),
    pytest.param(pr_flow_job.register_pr_flow_job, id="pr-flow"),
    pytest.param(walg_job.register_walg_job, id="walg"),
]
_UNREGISTER_CALLS: dict[str, Callable[[], None]] = {
    "health-probe": cron.unregister_os_cron,
    "autostart": autostart.unregister_autostart,
    "logs-maintenance": logs_job.unregister_logs_job,
    "packages-refresh": packages_job.unregister_packages_job,
    "pr-flow": pr_flow_job.unregister_pr_flow_job,
    "walg": walg_job.unregister_walg_job,
}
_UNREGISTERS = [pytest.param(call, id=name) for name, call in _UNREGISTER_CALLS.items()]


@pytest.mark.parametrize("call", _REGISTERS)
def test_registration_never_reaches_the_backend_when_gated(
    call: object, default_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("base.host.system.backend.get_backend", _ExplodingBackend)
    assert callable(call)
    call()  # no AssertionError == the gate held


def test_registration_dispatches_when_enabled(
    gate_on: None, default_home: Path, backend: _RecordingBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pr_flow_job, "credential_blocker", lambda: None)
    cron.register_os_cron()
    autostart.register_autostart()
    logs_job.register_logs_job()
    packages_job.register_packages_job()
    pr_flow_job.register_pr_flow_job()
    walg_job.register_walg_job()
    assert backend.calls == [
        "cron",
        "autostart",
        "logs-maintenance",
        "packages-refresh",
        "pr-flow",
        "walg",
    ]


def test_deregistration_is_not_gated_by_the_os_jobs_switch(
    default_home: Path, backend: _RecordingBackend
) -> None:
    """Cleanup has to work wherever registration is switched off (the suite runs
    with it off) — otherwise a leak under an older build can never be swept."""
    for call in _UNREGISTER_CALLS.values():
        call()
    assert backend.calls == [
        "unregister-cron",
        "unregister-autostart",
        "unregister-logs-maintenance",
        "unregister-packages-refresh",
        "unregister-pr-flow",
        "unregister-walg",
    ]


# ---------------------------------------------------------------------------
# The default-home gate
# ---------------------------------------------------------------------------
# Labels and crontab markers name a job, not a home, so the scheduler's one
# namespace per OS user is shared by every process on the host. A scratch home
# (a test, a tool, a disposable cluster) must neither replace nor remove the
# host's real job — on a development machine that also runs production, a
# `launchctl bootout` from a test would take a production job down.


@pytest.mark.parametrize("call", _REGISTERS)
def test_a_scratch_home_registers_nothing(
    call: object, gate_on: None, unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("base.host.system.backend.get_backend", _ExplodingBackend)
    monkeypatch.setattr(pr_flow_job, "credential_blocker", lambda: None)
    assert callable(call)
    call()  # no AssertionError == nothing reached the scheduler


@pytest.mark.parametrize("call", _UNREGISTERS)
def test_a_scratch_home_removes_nothing(
    call: object, unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("base.host.system.backend.get_backend", _ExplodingBackend)
    assert callable(call)
    call()


def test_the_default_home_owns_the_jobs_however_it_is_spelled(
    default_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cron.owns_os_jobs("autostart")
    monkeypatch.setenv("AVA_HOME", str(default_home / "elsewhere" / ".." / ".ava"))
    assert cron.owns_os_jobs("autostart")
    monkeypatch.setenv("AVA_HOME", str(default_home / ".ava-preview"))
    assert not cron.owns_os_jobs("autostart")


# ---------------------------------------------------------------------------
# What a generated job spec is anchored to
# ---------------------------------------------------------------------------


def test_ava_binary_path_prefers_this_checkout_over_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """PATH belongs to whoever launched the process; the job must run the binary
    of the checkout that owns `$AVA_HOME`. A leaked e2e job named a worktree's
    `ava` resolved off `uv run`'s PATH — one `shutil.which` hit away from naming
    prod's."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _n: "/somewhere/else/bin/ava")  # pyright: ignore[reportUnknownArgumentType]
    resolved = Path(cron.ava_binary_path())
    assert resolved.parent.parent.parent == Path(__file__).resolve().parents[3]
    assert resolved.parent.parent.name == ".venv"


def test_ava_binary_path_falls_back_to_path_without_a_venv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import shutil

    monkeypatch.setattr("base.paths.repo_root", lambda: tmp_path)
    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/local/bin/ava")  # pyright: ignore[reportUnknownArgumentType]
    assert cron.ava_binary_path() == "/usr/local/bin/ava"


def test_health_probe_plist_pins_ava_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An OS probe observes its owning home and carries no release policy."""
    monkeypatch.setenv("AVA_HOME", str(tmp_path / ".ava-x"))
    monkeypatch.setattr(cron, "ava_binary_path", lambda: "/x/ava")
    body = cron._launchd_plist_content(300)
    import plistlib

    assert plistlib.loads(body.encode())["ProgramArguments"] == [
        "/x/ava",
        "cluster",
        "health-probe",
    ]
    assert "<key>AVA_HOME</key>" in body
    assert f"<string>{tmp_path / '.ava-x'}</string>" in body
    assert "<key>PATH</key>" in body


@pytest.mark.parametrize(
    "render",
    [
        pytest.param(autostart._autostart_plist_content, id="autostart"),
    ],
)
def test_every_launchagent_pins_ava_home(
    render: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AVA_HOME", str(tmp_path / ".ava-x"))
    monkeypatch.setattr(cron, "ava_binary_path", lambda: "/x/ava")
    monkeypatch.setattr(autostart, "ava_binary_path", lambda: "/x/ava")
    assert callable(render)
    body = render()
    assert isinstance(body, str)
    assert f"<key>AVA_HOME</key>\n        <string>{tmp_path / '.ava-x'}</string>" in body


def test_cron_env_prefix_scopes_to_one_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`AVA_HOME=<home> <cmd>` and not a bare `AVA_HOME=` line: cron applies a
    standalone assignment to the WHOLE crontab, which would silently retarget a
    co-located cluster's entries."""
    monkeypatch.setenv("AVA_HOME", str(tmp_path / ".ava-x"))
    prefix = cron.cron_env_prefix()
    assert prefix == f"AVA_HOME={tmp_path / '.ava-x'} "
    assert not prefix.startswith("\n")


@pytest.mark.parametrize("boundary", ["converge", "build", "sign", "launchd"])
def test_helper_native_effects_require_explicit_test_boundary(boundary: str) -> None:
    from services import permissions_helper
    from services.permissions_helper import launchd_job, lifecycle

    with pytest.raises(pytest.fail.Exception, match="native effect forbidden"):
        if boundary == "converge":
            permissions_helper.converge()
        elif boundary == "build":
            lifecycle._run(["swiftc", "unreachable.swift"])
        elif boundary == "sign":
            lifecycle._run(["codesign", "--sign", "unreachable"])
        elif boundary == "launchd":
            launchd_job._retirement_command(["bootout", "gui/0/unreachable"], float("inf"))


@pytest.mark.native_permissions_helper
def test_native_helper_opt_in_exposes_real_boundaries_without_running_them() -> None:
    from base.host.proc import run_bounded
    from services import permissions_helper
    from services.permissions_helper import launchd_job, lifecycle

    assert permissions_helper.converge.__module__ == "services.permissions_helper"
    assert lifecycle.run_bounded is run_bounded
    assert launchd_job.run_bounded is run_bounded
