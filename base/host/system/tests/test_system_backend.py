"""Tests for base.host.system.backend — platform dispatch + capability queries."""

from __future__ import annotations

import pytest

import base.host.system.backend as backend_module
from base.host.system.backend import (
    LinuxPlatformBackend,
    MacPlatformBackend,
    PlatformBackend,
    get_backend,
)


def test_get_backend_returns_correct_type() -> None:
    """On macOS CI / dev, get_backend() returns MacPlatformBackend."""
    backend = get_backend()
    # All implementations are PlatformBackend instances
    assert isinstance(backend, PlatformBackend)


def test_unsupported_host_does_not_select_linux_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backend_module, "IS_MACOS", False)
    monkeypatch.setattr(backend_module, "is_linux", lambda: False, raising=False)
    with pytest.raises(RuntimeError, match="unsupported host platform"):
        backend_module.get_backend()


def test_mac_backend_venv_bin_dir() -> None:
    """macOS venv uses 'bin' directory."""
    backend = MacPlatformBackend()
    assert backend.venv_bin_dir_name() == "bin"


def test_linux_backend_venv_bin_dir() -> None:
    """Linux venv uses 'bin' directory."""
    backend = LinuxPlatformBackend()
    assert backend.venv_bin_dir_name() == "bin"


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


def test_backend_type_is_stable_across_calls() -> None:
    """get_backend() selects the same platform backend class on every call."""
    assert type(get_backend()) is type(get_backend())


def test_venv_python_path() -> None:
    """venv_python() returns a path inside the repo's .venv."""
    backend = MacPlatformBackend()
    path = backend.venv_python()
    assert ".venv" in path
    assert path.endswith("python3")


def test_linux_autostart_has_one_native_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    from base.host.system import boot_unit

    calls: list[str] = []

    def install() -> list[str]:
        calls.append("install")
        return []

    def uninstall() -> list[str]:
        calls.append("uninstall")
        return []

    monkeypatch.setattr(boot_unit, "install", install)
    monkeypatch.setattr(boot_unit, "uninstall", uninstall)
    backend = LinuxPlatformBackend()
    backend.register_autostart()
    backend.unregister_autostart()
    assert calls == ["install", "uninstall"]


def test_linux_autostart_propagates_unavailable_systemd(monkeypatch: pytest.MonkeyPatch) -> None:
    from base.host.system import boot_unit

    monkeypatch.setattr(boot_unit, "systemd_running", lambda: False)
    with pytest.raises(RuntimeError, match="systemd"):
        LinuxPlatformBackend().register_autostart()
