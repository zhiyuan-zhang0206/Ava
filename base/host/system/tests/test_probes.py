"""base.host.system.probes — the single source of truth for the host's Chrome
binary resolution, display detection, and AF_UNIX availability. These pure
probes are patched-against here (monkeypatching the module's sys / socket /
Path / shutil / settings) and consumed by services.desktop.browser.daemon,
ava.mcps.config, services.supervision.roster.spec, and base.host.config_validators.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

import base.host.system.probes as pp
from base.host import proc

# ─── resolve_chrome_binary ───────────────────────────────────────────────


def test_resolve_uses_explicit_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An explicit settings.services.chrome_binary is returned as-is, with NO exists()
    check (the browser daemon execs it and fails loudly if wrong)."""
    fake = tmp_path / "my-chrome"  # need not exist — returned verbatim
    monkeypatch.setattr(pp, "sys", _FakeSys("darwin"))
    monkeypatch.setattr(_settings_for(monkeypatch), "chrome_binary", str(fake))
    assert pp.resolve_chrome_binary() == str(fake)


def test_resolve_macos_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_settings_for(monkeypatch), "chrome_binary", None)
    monkeypatch.setattr(pp, "sys", _FakeSys("darwin"))
    monkeypatch.setattr(pp.Path, "exists", lambda _self: True)  # pyright: ignore[reportUnknownArgumentType]
    assert pp.resolve_chrome_binary() == pp._MACOS_CHROME


def test_resolve_returns_none_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_settings_for(monkeypatch), "chrome_binary", None)
    monkeypatch.setattr(pp, "sys", _FakeSys("darwin"))
    monkeypatch.setattr(pp.Path, "exists", lambda _self: False)  # pyright: ignore[reportUnknownArgumentType]
    assert pp.resolve_chrome_binary() is None


def test_resolve_linux_uses_which(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_settings_for(monkeypatch), "chrome_binary", None)
    monkeypatch.setattr(pp, "sys", _FakeSys("linux"))
    monkeypatch.setattr(
        pp.shutil,
        "which",
        lambda name: "/usr/bin/google-chrome" if name == "google-chrome" else None,  # pyright: ignore[reportUnknownArgumentType]
    )
    assert pp.resolve_chrome_binary() == "/usr/bin/google-chrome"


def test_resolve_linux_none_when_not_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_settings_for(monkeypatch), "chrome_binary", None)
    monkeypatch.setattr(pp, "sys", _FakeSys("linux"))
    monkeypatch.setattr(pp.shutil, "which", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]
    assert pp.resolve_chrome_binary() is None


# ─── default_chrome_user_data_dir ────────────────────────────────────────


def test_default_user_data_dir_macos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pp, "sys", _FakeSys("darwin"))
    monkeypatch.setattr(pp.Path, "home", classmethod(lambda _cls: tmp_path))  # pyright: ignore[reportUnknownArgumentType]
    expected = tmp_path / "Library" / "Application Support" / "Google" / "Chrome"
    expected.mkdir(parents=True)
    assert pp.default_chrome_user_data_dir() == expected


def test_default_user_data_dir_linux(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pp, "sys", _FakeSys("linux"))
    monkeypatch.setattr(pp.Path, "home", classmethod(lambda _cls: tmp_path))  # pyright: ignore[reportUnknownArgumentType]
    expected = tmp_path / ".config" / "google-chrome"
    expected.mkdir(parents=True)
    assert pp.default_chrome_user_data_dir() == expected


def test_default_user_data_dir_none_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(pp, "sys", _FakeSys("darwin"))
    monkeypatch.setattr(pp.Path, "home", classmethod(lambda _cls: tmp_path))  # pyright: ignore[reportUnknownArgumentType]
    assert pp.default_chrome_user_data_dir() is None  # dir never created


def test_default_user_data_dir_none_on_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pp, "sys", _FakeSys("freebsd"))
    assert pp.default_chrome_user_data_dir() is None


# ─── display_available ───────────────────────────────────────────────────


def test_display_true_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp, "sys", _FakeSys("darwin"))
    assert pp.display_available() is True


def test_display_linux_requires_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp, "sys", _FakeSys("linux"))
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert pp.display_available() is False
    monkeypatch.setenv("DISPLAY", ":0")
    assert pp.display_available() is True
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert pp.display_available() is True


def test_display_false_on_unknown_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown (non-macOS/Linux) platform fails safe to no display."""
    monkeypatch.setattr(pp, "sys", _FakeSys("freebsd"))
    assert pp.display_available() is False


# ─── browser_incapability / browser_capable ──────────────────────────────


def _all_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make all three browser prongs (display + Chrome + npx) pass."""
    monkeypatch.setattr(pp, "display_available", lambda: True)
    monkeypatch.setattr(pp, "resolve_chrome_binary", lambda: "/chrome")
    monkeypatch.setattr(pp.shutil, "which", lambda _name: "/usr/bin/npx")  # pyright: ignore[reportUnknownArgumentType]


def test_incapability_none_when_all_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _all_present(monkeypatch)
    assert pp.browser_incapability() is None
    assert pp.browser_capable() is True


def test_incapability_no_display(monkeypatch: pytest.MonkeyPatch) -> None:
    _all_present(monkeypatch)
    monkeypatch.setattr(pp, "display_available", lambda: False)
    assert pp.browser_incapability() == "no display (headless server)"
    assert pp.browser_capable() is False


def test_incapability_no_chrome(monkeypatch: pytest.MonkeyPatch) -> None:
    _all_present(monkeypatch)
    monkeypatch.setattr(pp, "resolve_chrome_binary", lambda: None)
    assert pp.browser_incapability() == "no Chrome (install it or set AVA_CHROME_BINARY)"
    assert pp.browser_capable() is False


def test_incapability_no_npx(monkeypatch: pytest.MonkeyPatch) -> None:
    _all_present(monkeypatch)
    monkeypatch.setattr(pp.shutil, "which", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]
    assert pp.browser_incapability() == "no npx (install Node.js for chrome-devtools-mcp)"
    assert pp.browser_capable() is False


def test_incapability_checks_display_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Order is display -> Chrome -> npx; the first missing prong wins, so a fully
    bare host reports the display reason (not a misleading Chrome/npx one)."""
    monkeypatch.setattr(pp, "display_available", lambda: False)
    monkeypatch.setattr(pp, "resolve_chrome_binary", lambda: None)
    monkeypatch.setattr(pp.shutil, "which", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]
    assert pp.browser_incapability() == "no display (headless server)"


# ─── browser_deps_incapability ───────────────────────────────────────────


def test_npx_incapability_reason_is_exported_once_for_both_probe_variants() -> None:
    """Changing the npx operator guidance must not split runtime and converge probes."""
    assert getattr(pp, "NPX_INCAPABILITY_REASON", None) == (
        "no npx (install Node.js for chrome-devtools-mcp)"
    )


def _all_platform_browser_deps_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the settings-free browser dependency prongs pass."""
    monkeypatch.setattr(pp, "display_available", lambda: True)
    monkeypatch.setattr(pp, "_platform_chrome_binary", lambda: "/chrome")
    monkeypatch.setattr(pp.shutil, "which", lambda _name: "/usr/bin/npx")  # pyright: ignore[reportUnknownArgumentType]


def test_browser_deps_incapability_none_when_all_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _all_platform_browser_deps_present(monkeypatch)
    assert pp.browser_deps_incapability() is None


def test_browser_deps_incapability_returns_display_reason_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _all_platform_browser_deps_present(monkeypatch)
    monkeypatch.setattr(pp, "display_available", lambda: False)
    assert pp.browser_deps_incapability() == "no display (headless server)"


def test_browser_deps_incapability_returns_chrome_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    _all_platform_browser_deps_present(monkeypatch)
    monkeypatch.setattr(pp, "_platform_chrome_binary", lambda: None)
    assert pp.browser_deps_incapability() == "no Chrome (install it or set AVA_CHROME_BINARY)"


def test_browser_deps_incapability_returns_npx_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    _all_platform_browser_deps_present(monkeypatch)
    monkeypatch.setattr(pp.shutil, "which", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]
    assert pp.browser_deps_incapability() == "no npx (install Node.js for chrome-devtools-mcp)"


# ─── gui_session_domain / gui_login_user (task #3346) ────────────────────


def _unexpected_probe(argv: list[str]) -> str | None:
    raise AssertionError(f"probe should not run here: {argv}")


def test_gui_session_domain_reads_launchctl_managername(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp, "sys", _FakeSys("darwin"))
    calls: list[list[str]] = []
    monkeypatch.setattr(
        pp,
        "_bounded_stdout",
        lambda argv: calls.append(argv) or "Background",  # pyright: ignore[reportUnknownArgumentType]
    )
    assert pp.gui_session_domain() == "Background"
    assert calls == [["/bin/launchctl", "managername"]]


def test_gui_session_domain_none_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp, "sys", _FakeSys("linux"))
    monkeypatch.setattr(pp, "_bounded_stdout", _unexpected_probe)
    assert pp.gui_session_domain() is None


def test_gui_login_user_reads_the_console_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp, "sys", _FakeSys("darwin"))
    calls: list[list[str]] = []
    monkeypatch.setattr(
        pp,
        "_bounded_stdout",
        lambda argv: calls.append(argv) or "zyonzhang",  # pyright: ignore[reportUnknownArgumentType]
    )
    assert pp.gui_login_user() == "zyonzhang"
    assert calls == [["/usr/bin/stat", "-f%Su", "/dev/console"]]


def test_gui_login_user_none_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp, "sys", _FakeSys("linux"))
    monkeypatch.setattr(pp, "_bounded_stdout", _unexpected_probe)
    assert pp.gui_login_user() is None


class _Completed:
    """Minimal CompletedProcess stand-in for the bounded runner."""

    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


def _runner_returning(completed: _Completed) -> Callable[..., _Completed]:
    def _run(*_args: object, **_kwargs: object) -> _Completed:
        return completed

    return _run


def test_bounded_stdout_returns_the_stripped_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proc, "run_bounded", _runner_returning(_Completed(0, " Aqua\n")))
    assert pp._bounded_stdout(["/bin/launchctl", "managername"]) == "Aqua"


def test_bounded_stdout_none_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proc, "run_bounded", _runner_returning(_Completed(1, "error")))
    assert pp._bounded_stdout(["/bin/launchctl", "managername"]) is None


def test_bounded_stdout_none_on_empty_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proc, "run_bounded", _runner_returning(_Completed(0, "  \n")))
    assert pp._bounded_stdout(["/bin/launchctl", "managername"]) is None


def test_bounded_stdout_none_on_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def _missing(*_a: object, **_k: object) -> None:
        raise FileNotFoundError("no launchctl")

    monkeypatch.setattr(proc, "run_bounded", _missing)
    assert pp._bounded_stdout(["/bin/launchctl", "managername"]) is None


def test_bounded_stdout_none_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def _timeout(*_a: object, **_k: object) -> None:
        raise subprocess.TimeoutExpired(["launchctl"], 5.0)

    monkeypatch.setattr(proc, "run_bounded", _timeout)
    assert pp._bounded_stdout(["/bin/launchctl", "managername"]) is None


# ─── helpers ─────────────────────────────────────────────────────────────


class _FakeSys:
    """Minimal stand-in for the `sys` module exposing only `.platform`.

    `display_available` / `resolve_chrome_binary` only read `sys.platform`;
    patching the whole module attribute is cleaner than mutating the real one.
    """

    def __init__(self, platform: str) -> None:
        self.platform = platform


def _settings_for(monkeypatch: pytest.MonkeyPatch):
    """Return the sub-model holding chrome_binary (settings.services), the target
    these tests monkeypatch. The probes import settings lazily."""
    from base.config import settings

    return settings.services


def test_runtime_and_settings_free_probes_share_the_npx_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed npx message cannot split runtime gating from enrollment repair."""
    monkeypatch.setattr(pp, "display_available", lambda: True)
    monkeypatch.setattr(pp, "resolve_chrome_binary", lambda: "/chrome")
    monkeypatch.setattr(pp, "_platform_chrome_binary", lambda: "/chrome")
    monkeypatch.setattr(
        pp.shutil,
        "which",
        lambda _name: None,  # pyright: ignore[reportUnknownArgumentType]
    )
    assert pp.browser_incapability() == pp.NPX_INCAPABILITY_REASON
    assert pp.browser_deps_incapability() == pp.NPX_INCAPABILITY_REASON
