from __future__ import annotations

import pytest

import base.host.system.backend as backend_module
from base.host.system import autostart
from base.host.system.backend import (
    LinuxPlatformBackend,
    MacPlatformBackend,
    PlatformBackend,
    get_backend,
)
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import LINUX_X86, fake_host


def test_get_backend_returns_correct_type() -> None:
    backend = get_backend()
    assert isinstance(backend, PlatformBackend)


def test_unsupported_host_does_not_select_linux_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backend_module, "is_macos", lambda: False)
    monkeypatch.setattr(backend_module, "is_linux", lambda: False, raising=False)
    with pytest.raises(RuntimeError, match="unsupported host platform"):
        backend_module.get_backend()


def test_mac_backend_venv_bin_dir() -> None:
    backend = MacPlatformBackend()
    assert backend.venv_bin_dir_name() == "bin"


def test_linux_backend_venv_bin_dir() -> None:
    backend = LinuxPlatformBackend()
    assert backend.venv_bin_dir_name() == "bin"


def test_backend_type_is_stable_across_calls() -> None:
    assert type(get_backend()) is type(get_backend())


def test_linux_autostart_has_one_native_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def install(*, host: Host) -> list[str]:
        calls.append("install")
        return []

    def uninstall(*, host: Host) -> list[str]:
        calls.append("uninstall")
        return []

    monkeypatch.setattr(autostart, "install", install)
    monkeypatch.setattr(autostart, "uninstall", uninstall)
    autostart.register_linux(host=fake_host(platform=LINUX_X86))
    autostart.unregister_linux(host=fake_host(platform=LINUX_X86))
    assert calls == ["install", "uninstall"]


def test_linux_autostart_propagates_unavailable_systemd(monkeypatch: pytest.MonkeyPatch) -> None:
    from base.host.system import boot_unit

    def no_systemd(*, host: Host) -> bool:
        return False

    monkeypatch.setattr(boot_unit, "systemd_running", no_systemd)
    with pytest.raises(RuntimeError, match="systemd"):
        autostart.register_linux(host=fake_host(platform=LINUX_X86))
