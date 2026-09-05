"""The `AVA_OS_JOBS_ENABLED` gate, and what a generated job spec is anchored to.

Together these are the fix for the e2e OS-job leak: the gate stops the suite from
arming a job at all, and the anchoring makes any job that IS armed name the
binary and `$AVA_HOME` of the checkout that wrote it — so no stale job can
resolve onto the prod install.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shared import os_autostart, os_cron, os_watchdog_probe
from shared.config import settings


class _ExplodingBackend:
    """A backend whose every registration path is a test failure."""

    def register_cron(self, **_kw: object) -> None:
        raise AssertionError("register_cron reached the OS with the gate off")

    def register_autostart(self) -> None:
        raise AssertionError("register_autostart reached the OS with the gate off")

    def register_watchdog_probe(self, _role: str, **_kw: object) -> None:
        raise AssertionError("register_watchdog_probe reached the OS with the gate off")


class _RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def register_cron(self, **_kw: object) -> None:
        self.calls.append("cron")

    def register_autostart(self) -> None:
        self.calls.append("autostart")

    def register_watchdog_probe(self, role: str, **_kw: object) -> None:
        self.calls.append(f"watchdog-probe.{role}")

    def unregister_cron(self, slug: str) -> None:
        self.calls.append(f"unregister-cron:{slug}")

    def unregister_autostart(self, slug: str) -> None:
        self.calls.append(f"unregister-autostart:{slug}")

    def unregister_watchdog_probe(self, role: str, slug: str) -> None:
        self.calls.append(f"unregister-watchdog-probe:{role}:{slug}")


@pytest.fixture()
def backend(monkeypatch: pytest.MonkeyPatch) -> _RecordingBackend:
    rec = _RecordingBackend()
    monkeypatch.setattr("shared.platform_backend.get_backend", lambda: rec)
    return rec


@pytest.fixture()
def gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite pins the gate OFF for every test (tests/conftest.py); the few
    cases that assert the ENABLED behaviour turn it back on for themselves."""
    monkeypatch.setattr(settings.general, "os_jobs_enabled", True)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_suite_default_is_off() -> None:
    """The whole suite runs with registration disabled — this is the invariant the
    e2e leak violated (nine launchd health probes on a dev box), so assert it
    directly rather than trusting the conftest comment."""
    assert os_cron.os_jobs_enabled() is False


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(os_cron.register_os_cron, id="health-probe"),
        pytest.param(os_autostart.register_autostart, id="autostart"),
        pytest.param(lambda: os_watchdog_probe.register_watchdog_probe("gateway"), id="watchdog"),
    ],
)
def test_registration_never_reaches_the_backend_when_gated(
    call: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shared.platform_backend.get_backend", _ExplodingBackend)
    assert callable(call)
    call()  # no AssertionError == the gate held


def test_registration_dispatches_when_enabled(gate_on: None, backend: _RecordingBackend) -> None:
    os_cron.register_os_cron()
    os_autostart.register_autostart()
    os_watchdog_probe.register_watchdog_probe("agent-runner")
    assert backend.calls == ["cron", "autostart", "watchdog-probe.agent-runner"]


def test_deregistration_is_never_gated(backend: _RecordingBackend, tmp_path: Path) -> None:
    """Cleanup has to work wherever registration is forbidden — otherwise a run
    that leaks under an older build can never be swept by a newer one."""
    home = tmp_path / ".ava-target"
    os_cron.unregister_os_cron(home)
    os_autostart.unregister_autostart(home)
    os_watchdog_probe.unregister_watchdog_probe("gateway", home)
    assert [c.split(":")[0] for c in backend.calls] == [
        "unregister-cron",
        "unregister-autostart",
        "unregister-watchdog-probe",
    ]


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
    resolved = Path(os_cron.ava_binary_path())
    assert resolved.parent.parent.parent == Path(__file__).resolve().parents[2]
    assert resolved.parent.parent.name == ".venv"


def test_ava_binary_path_falls_back_to_path_without_a_venv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import shutil

    monkeypatch.setattr("shared.paths.repo_root", lambda: tmp_path)
    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/local/bin/ava")  # pyright: ignore[reportUnknownArgumentType]
    assert os_cron.ava_binary_path() == "/usr/local/bin/ava"


def test_health_probe_plist_pins_ava_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The leaked plists set no environment at all, so the probe they ran would
    have resolved `$AVA_HOME` to the PROD home and driven `--auto-rollback`
    there."""
    monkeypatch.setattr(settings.general, "ava_home", tmp_path / ".ava-x")
    monkeypatch.setattr(os_cron, "ava_binary_path", lambda: "/x/ava")
    monkeypatch.setattr(os_cron, "_home_slug", lambda: "ava-x")
    body = os_cron._launchd_plist_content(300, 3)
    assert "<key>AVA_HOME</key>" in body
    assert f"<string>{tmp_path / '.ava-x'}</string>" in body
    assert "<key>PATH</key>" in body


@pytest.mark.parametrize(
    "render",
    [
        pytest.param(os_autostart._autostart_plist_content, id="autostart"),
        pytest.param(lambda: os_watchdog_probe._plist_content("gateway", 60), id="watchdog"),
    ],
)
def test_every_launchagent_pins_ava_home(
    render: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings.general, "ava_home", tmp_path / ".ava-x")
    monkeypatch.setattr(os_cron, "ava_binary_path", lambda: "/x/ava")
    monkeypatch.setattr(os_autostart, "ava_binary_path", lambda: "/x/ava")
    monkeypatch.setattr(os_watchdog_probe, "ava_binary_path", lambda: "/x/ava")
    monkeypatch.setattr(os_autostart, "_home_slug", lambda: "ava-x")
    monkeypatch.setattr(os_watchdog_probe, "_home_slug", lambda: "ava-x")
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
    monkeypatch.setattr(settings.general, "ava_home", tmp_path / ".ava-x")
    prefix = os_cron.cron_env_prefix()
    assert prefix == f"AVA_HOME={tmp_path / '.ava-x'} "
    assert not prefix.startswith("\n")
