from pathlib import Path

import pytest

import base.host.system.probes as pp
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import (
    DARWIN_ARM64,
    LINUX_X86,
    fake_host,
)

_CHROME = "/usr/bin/google-chrome"
_NPX = "/usr/bin/npx"


def _linux(programs: dict[str, str] | None = None) -> Host:
    return fake_host(platform=LINUX_X86, programs=programs)


def _display(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    """Give the process an X display, or take every display variable away."""
    if on:
        monkeypatch.setenv("DISPLAY", ":0")
    else:
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)


def _desktop_linux(monkeypatch: pytest.MonkeyPatch, programs: dict[str, str] | None = None) -> Host:
    """A Linux host with a display, Chrome and npx unless `programs` says otherwise."""
    _display(monkeypatch, on=True)
    return _linux(
        {"google-chrome": _CHROME, "npx": _NPX} if programs is None else programs,
    )


def _path_exists(_self: Path) -> bool:
    return True


def _path_missing(_self: Path) -> bool:
    return False


# ─── resolve_chrome_binary ───────────────────────────────────────────────


def test_resolve_uses_explicit_override(tmp_path: Path) -> None:
    """An explicit configured chrome binary is returned as-is, with NO exists()
    check (the browser daemon execs it and fails loudly if wrong)."""
    fake = tmp_path / "my-chrome"  # need not exist — returned verbatim
    assert pp.resolve_chrome_binary(str(fake), host=fake_host(platform=DARWIN_ARM64)) == str(fake)


def test_resolve_macos_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp.Path, "exists", _path_exists)  # pyright: ignore[reportUnknownArgumentType]
    host = fake_host(platform=DARWIN_ARM64)
    assert pp.resolve_chrome_binary(None, host=host) == pp.MACOS_CHROME


def test_resolve_returns_none_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp.Path, "exists", _path_missing)  # pyright: ignore[reportUnknownArgumentType]
    assert pp.resolve_chrome_binary(None, host=fake_host(platform=DARWIN_ARM64)) is None


def test_resolve_linux_uses_which() -> None:
    host = _linux({"google-chrome": _CHROME})
    assert pp.resolve_chrome_binary(None, host=host) == _CHROME


def test_resolve_linux_falls_back_to_the_stable_channel_name() -> None:
    host = _linux({"google-chrome-stable": "/opt/chrome/stable"})
    assert pp.resolve_chrome_binary(None, host=host) == "/opt/chrome/stable"


def test_resolve_linux_none_when_not_on_path() -> None:
    assert pp.resolve_chrome_binary(None, host=_linux()) is None


# ─── default_chrome_user_data_dir ────────────────────────────────────────


def test_default_user_data_dir_macos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pp.Path, "home", classmethod(lambda _cls: tmp_path))  # pyright: ignore[reportUnknownArgumentType]
    expected = tmp_path / "Library" / "Application Support" / "Google" / "Chrome"
    expected.mkdir(parents=True)
    assert pp.default_chrome_user_data_dir(host=fake_host(platform=DARWIN_ARM64)) == expected


def test_default_user_data_dir_linux(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pp.Path, "home", classmethod(lambda _cls: tmp_path))  # pyright: ignore[reportUnknownArgumentType]
    expected = tmp_path / ".config" / "google-chrome"
    expected.mkdir(parents=True)
    assert pp.default_chrome_user_data_dir(host=_linux()) == expected


def test_default_user_data_dir_none_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(pp.Path, "home", classmethod(lambda _cls: tmp_path))  # pyright: ignore[reportUnknownArgumentType]
    assert pp.default_chrome_user_data_dir(host=fake_host(platform=DARWIN_ARM64)) is None


# ─── display_available ───────────────────────────────────────────────────


def test_display_true_on_macos() -> None:
    assert pp.display_available(host=fake_host(platform=DARWIN_ARM64)) is True


def test_display_linux_requires_env(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _linux()
    _display(monkeypatch, on=False)
    assert pp.display_available(host=host) is False
    _display(monkeypatch, on=True)
    assert pp.display_available(host=host) is True
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert pp.display_available(host=host) is True


# ─── browser_incapability / browser_capable ──────────────────────────────


def test_incapability_none_when_all_present(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _desktop_linux(monkeypatch)
    assert pp.browser_incapability(None, host=host) is None
    assert pp.browser_capable(None, host=host) is True


def test_incapability_accepts_a_configured_chrome_without_one_on_the_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _desktop_linux(monkeypatch, {"npx": _NPX})
    assert pp.browser_incapability("/opt/my-chrome", host=host) is None


def test_incapability_no_display(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _desktop_linux(monkeypatch)
    _display(monkeypatch, on=False)
    assert pp.browser_incapability(None, host=host) == "no display (headless server)"
    assert pp.browser_capable(None, host=host) is False


def test_incapability_no_chrome(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _desktop_linux(monkeypatch, {"npx": _NPX})
    assert (
        pp.browser_incapability(None, host=host)
        == "no Chrome (install it or set AVA_CHROME_BINARY)"
    )
    assert pp.browser_capable(None, host=host) is False


def test_incapability_no_npx(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _desktop_linux(monkeypatch, {"google-chrome": _CHROME})
    assert (
        pp.browser_incapability(None, host=host)
        == "no npx (install Node.js for chrome-devtools-mcp)"
    )
    assert pp.browser_capable(None, host=host) is False


def test_incapability_checks_display_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Order is display -> Chrome -> npx; the first missing prong wins, so a fully
    bare host reports the display reason (not a misleading Chrome/npx one)."""
    _display(monkeypatch, on=False)
    assert pp.browser_incapability(None, host=_linux()) == "no display (headless server)"


def test_npx_incapability_reason_is_exported_once() -> None:
    assert getattr(pp, "NPX_INCAPABILITY_REASON", None) == (
        "no npx (install Node.js for chrome-devtools-mcp)"
    )


# ─── permissions_helper_incapability ─────────────────────────────────────


def test_permissions_helper_is_macos_only() -> None:
    reason = pp.permissions_helper_incapability(host=_linux())
    assert reason == "macOS only (permissions helper drives the desktop)"


def test_permissions_helper_needs_the_swift_toolchain_and_signing() -> None:
    both = {"swiftc": "/usr/bin/swiftc", "codesign": "/usr/bin/codesign"}
    assert pp.permissions_helper_incapability(host=fake_host(platform=DARWIN_ARM64)) == (
        "no swiftc (install the Xcode command line tools)"
    )
    only_swift = fake_host(platform=DARWIN_ARM64, programs={"swiftc": both["swiftc"]})
    assert pp.permissions_helper_incapability(host=only_swift) == (
        "no codesign (install the Xcode command line tools)"
    )
    complete = fake_host(platform=DARWIN_ARM64, programs=both)
    assert pp.permissions_helper_incapability(host=complete) is None
