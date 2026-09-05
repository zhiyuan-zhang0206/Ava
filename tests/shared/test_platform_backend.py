"""Tests for shared.platform_backend — singleton dispatch + capability queries."""

from __future__ import annotations

import pytest

from shared.platform_backend import (
    LinuxPlatformBackend,
    MacPlatformBackend,
    PlatformBackend,
    WindowsPlatformBackend,
    get_backend,
)


def test_get_backend_returns_correct_type() -> None:
    """On macOS CI / dev, get_backend() returns MacPlatformBackend."""
    backend = get_backend()
    # All implementations are PlatformBackend instances
    assert isinstance(backend, PlatformBackend)


def test_mac_backend_venv_bin_dir() -> None:
    """macOS venv uses 'bin' directory."""
    backend = MacPlatformBackend()
    assert backend.venv_bin_dir_name() == "bin"


def test_linux_backend_venv_bin_dir() -> None:
    """Linux venv uses 'bin' directory."""
    backend = LinuxPlatformBackend()
    assert backend.venv_bin_dir_name() == "bin"


def test_windows_backend_venv_bin_dir() -> None:
    """Windows venv uses 'Scripts' directory."""
    backend = WindowsPlatformBackend()
    assert backend.venv_bin_dir_name() == "Scripts"


def test_mac_capability_queries() -> None:
    """macOS supports all standard capabilities."""
    backend = MacPlatformBackend()
    assert backend.supports_ava_symlink() is True
    assert backend.supports_shell_rc() is True
    assert backend.is_posix() is True
    assert backend.supports_data_plane() is True
    assert backend.npm_shell_flag() is False


def test_linux_capability_queries() -> None:
    """Linux supports all standard capabilities."""
    backend = LinuxPlatformBackend()
    assert backend.supports_ava_symlink() is True
    assert backend.supports_shell_rc() is True
    assert backend.is_posix() is True
    assert backend.supports_data_plane() is True
    assert backend.npm_shell_flag() is False


def test_windows_capability_queries() -> None:
    """Windows returns False for features not yet wired."""
    backend = WindowsPlatformBackend()
    assert backend.supports_ava_symlink() is False
    assert backend.supports_shell_rc() is False
    assert backend.is_posix() is False
    assert backend.supports_data_plane() is False
    assert backend.npm_shell_flag() is True


def test_windows_scheduling_delegates_to_schtasks(monkeypatch: pytest.MonkeyPatch) -> None:
    """All three job kinds route through the Task Scheduler backend.

    These used to be deliberate no-ops ("not yet wired on Windows"), which left a
    Windows unit with no health probe, no boot autostart, and — once the watchdog
    probe existed — no watchdog supervision either."""
    calls: list[str] = []
    unregistered: list[tuple[str, tuple[object, ...]]] = []
    for mod, name in [
        ("shared.os_autostart", "autostart"),
        ("shared.os_cron", "cron"),
        ("shared.os_watchdog_probe", "watchdog"),
    ]:
        monkeypatch.setattr(f"{mod}._register_windows", lambda *_a, _n=name: calls.append(_n) or 0)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(
            f"{mod}._unregister_windows",
            lambda *a, _n=name: unregistered.append((_n, a)) or 0,  # pyright: ignore[reportUnknownArgumentType]
        )

    backend = WindowsPlatformBackend()
    backend.register_autostart()
    backend.register_cron()
    backend.register_watchdog_probe("gateway")
    assert calls == ["autostart", "cron", "watchdog"]

    # Unregister stays silent-on-absent, like the launchd / crontab paths — and
    # carries the caller's slug instead of resolving one from this process, so
    # `ava cluster destroy` removes the target cluster's tasks and not its own.
    backend.unregister_autostart("ava-target")
    backend.unregister_cron("ava-target")
    backend.unregister_watchdog_probe("gateway", "ava-target")

    assert unregistered == [
        ("autostart", ("ava-target",)),
        ("cron", ("ava-target",)),
        ("watchdog", ("gateway", "ava-target")),
    ]


def test_windows_scheduling_failure_degrades_to_a_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed Windows registration must NOT fail the bring-up (the win
    2026-08-11 policy, task #1196): the failure class is transient (a retry
    inside `_register` already cleared the common case), a cluster that is down
    is worse than one that is up and loudly unsupervised, and every `ava start`
    retries. POSIX keeps failing fast — this is the Windows backend alone."""
    for mod, _name in [
        ("shared.os_autostart", "autostart"),
        ("shared.os_cron", "health probe"),
        ("shared.os_watchdog_probe", "watchdog probe"),
    ]:
        monkeypatch.setattr(f"{mod}._register_windows", lambda *_a: "ERROR: Access is denied.")  # pyright: ignore[reportUnknownArgumentType]

    backend = WindowsPlatformBackend()
    backend.register_autostart()
    backend.register_cron()
    backend.register_watchdog_probe("gateway")  # no exception

    err = capsys.readouterr().err
    assert "autostart" in err
    assert "health probe" in err
    assert "watchdog probe" in err
    # And each says WHY, on stderr. The loguru record alone never reached disk on
    # the fleet's Windows box — a converge under the updater chain has its stderr
    # captured into the updater log but no sink configured — so "registration
    # failed" with nothing after it is all nine months of logs ever showed.
    assert err.count("ERROR: Access is denied.") == 3


def test_windows_pg_binary_path() -> None:
    """Windows PG binary path includes .exe suffix."""
    backend = WindowsPlatformBackend()
    path = backend.pg_binary_path("pg_ctl")
    assert path is None or path.name == "pg_ctl.exe"


def test_singleton_returns_same_instance() -> None:
    """get_backend() is a singleton — same instance every call."""
    b1 = get_backend()
    b2 = get_backend()
    assert b1 is b2


def test_venv_python_path() -> None:
    """venv_python() returns a path inside the repo's .venv."""
    backend = MacPlatformBackend()
    path = backend.venv_python()
    assert ".venv" in path
    assert path.endswith("python3")
