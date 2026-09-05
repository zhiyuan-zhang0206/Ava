"""Settings-free browser dependency detection, repair, and operator guidance."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared import browser_deps, platform_probes

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("platform", "expected_tool"),
    [
        ("darwin", "brew"),
        ("linux", "apt-get"),
        ("win32", "winget"),
        ("freebsd", "nodejs.org"),
    ],
)
def test_node_install_command_names_the_platform_installer(
    monkeypatch: pytest.MonkeyPatch, platform: str, expected_tool: str
) -> None:
    """A missing npx warning gives this host a copy-pasteable repair command."""
    monkeypatch.setattr(browser_deps, "sys", SimpleNamespace(platform=platform))
    command = browser_deps.node_install_command()
    assert command
    assert expected_tool in command


def test_node_install_command_explains_the_macos_keg_only_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mac operators get a command that leaves npx on PATH after install."""
    monkeypatch.setattr(browser_deps, "sys", SimpleNamespace(platform="darwin"))
    assert browser_deps.node_install_command() == (
        "brew install node  (or: brew install node@22 && brew link --force node@22)"
    )


def test_install_nodejs_short_circuits_when_npx_is_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """An already-capable host never reruns the provisioner."""
    monkeypatch.setattr(browser_deps.shutil, "which", lambda _name: "/usr/bin/npx")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        browser_deps.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("provisioner ran despite npx on PATH"),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert browser_deps.install_nodejs() is True


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_install_nodejs_runs_the_shared_provisioner_and_rechecks_npx(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """POSIX repair uses install.sh's provisioner, then reports the real PATH state."""
    npx_available = False
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(browser_deps, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(
        browser_deps.shutil,
        "which",
        lambda _name: "/usr/bin/npx" if npx_available else None,  # pyright: ignore[reportUnknownArgumentType]
    )

    def run(argv: list[str], **kwargs: object) -> None:
        nonlocal npx_available
        calls.append((argv, kwargs))
        npx_available = True

    monkeypatch.setattr(browser_deps.subprocess, "run", run)
    assert browser_deps.install_nodejs() is True
    assert calls == [
        (
            ["bash", str(_REPO_ROOT / "scripts" / "provision" / "node.sh")],
            {"capture_output": True, "text": True, "timeout": 900, "check": False},
        )
    ]


def test_install_nodejs_does_not_silently_elevate_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows repair remains explicit because winget may require elevation."""
    monkeypatch.setattr(browser_deps, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(browser_deps.shutil, "which", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        browser_deps.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Windows must not run a silent installer"),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert browser_deps.install_nodejs() is False


def test_ensure_browser_deps_returns_none_when_host_is_already_capable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No repair is attempted when the settings-free probe is already clear."""
    monkeypatch.setattr(browser_deps, "browser_deps_incapability", lambda: None)
    monkeypatch.setattr(
        browser_deps,
        "install_nodejs",
        lambda: pytest.fail("repair attempted on a capable host"),
    )
    assert browser_deps.ensure_browser_deps() is None


def test_ensure_browser_deps_rechecks_after_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful repair is confirmed by the same capability probe, not assumed."""
    reasons = iter([platform_probes.NPX_INCAPABILITY_REASON, None])
    monkeypatch.setattr(browser_deps, "browser_deps_incapability", lambda: next(reasons))
    monkeypatch.setattr(browser_deps, "install_nodejs", lambda: True)
    assert browser_deps.ensure_browser_deps() is None


def test_ensure_browser_deps_returns_npx_reason_when_repair_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed best-effort repair keeps the actionable missing-npx reason."""
    monkeypatch.setattr(
        browser_deps, "browser_deps_incapability", lambda: platform_probes.NPX_INCAPABILITY_REASON
    )
    monkeypatch.setattr(browser_deps, "install_nodejs", lambda: False)
    assert browser_deps.ensure_browser_deps() == platform_probes.NPX_INCAPABILITY_REASON


def test_ensure_browser_deps_does_not_install_node_for_missing_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Display and Chrome are host properties, so only a missing npx triggers repair."""
    reason = "no display (WSL without WSLg / headless server)"
    monkeypatch.setattr(browser_deps, "browser_deps_incapability", lambda: reason)
    monkeypatch.setattr(
        browser_deps,
        "install_nodejs",
        lambda: pytest.fail("Node repair attempted for a non-Node dependency"),
    )
    assert browser_deps.ensure_browser_deps() == reason


def test_runtime_and_settings_free_probes_share_the_npx_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed npx message cannot split runtime gating from enrollment repair."""
    monkeypatch.setattr(platform_probes, "display_available", lambda: True)
    monkeypatch.setattr(platform_probes, "resolve_chrome_binary", lambda: "/chrome")
    monkeypatch.setattr(platform_probes, "_platform_chrome_binary", lambda: "/chrome")
    monkeypatch.setattr(
        platform_probes.shutil,
        "which",
        lambda _name: None,  # pyright: ignore[reportUnknownArgumentType]
    )
    assert platform_probes.browser_incapability() == platform_probes.NPX_INCAPABILITY_REASON
    assert platform_probes.browser_deps_incapability() == platform_probes.NPX_INCAPABILITY_REASON


def test_browser_deps_warning_names_the_npx_reason_and_platform_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The warning tells the operator both why ava-browser is skipped and how to fix it."""
    monkeypatch.setattr(browser_deps, "node_install_command", lambda: "install-node-here")
    warning = browser_deps.browser_deps_warning(platform_probes.NPX_INCAPABILITY_REASON)
    assert platform_probes.NPX_INCAPABILITY_REASON in warning
    assert "install-node-here" in warning
    assert "ava-browser will not run on this host until this is fixed" in warning


def test_browser_deps_warning_explains_that_a_missing_display_needs_no_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A display-less host must not receive a Node repair instruction."""
    monkeypatch.setattr(browser_deps, "node_install_command", lambda: "install-node-here")
    warning = browser_deps.browser_deps_warning("no display (WSL without WSLg / headless server)")
    assert "install-node-here" not in warning
    assert "nothing to" in warning
    assert "install for ava-browser" in warning


def test_browser_deps_warning_explains_how_to_repair_missing_chrome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chrome is the missing prong, so its repair—not Node's—must lead the box."""
    monkeypatch.setattr(browser_deps, "node_install_command", lambda: "install-node-here")
    warning = browser_deps.browser_deps_warning("no Chrome (install it or set AVA_CHROME_BINARY)")
    assert "install-node-here" not in warning
    assert "Install Google Chrome" in warning
    assert "AVA_CHROME_BINARY" in warning


def test_browser_deps_notice_marks_a_displayless_host_not_applicable() -> None:
    """Fresh enrollment names the deliberate browser skip instead of a repair alarm."""
    reason = "no display (WSL without WSLg / headless server)"
    assert hasattr(browser_deps, "browser_deps_notice")
    notice = browser_deps.browser_deps_notice(reason)
    assert "not applicable" in notice
    assert reason in notice


def test_browser_deps_module_never_imports_settings_or_cli() -> None:
    """Fresh-host enrollment can import this module without constructing Settings."""
    tree = ast.parse(inspect.getsource(browser_deps))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "shared.config" not in imported
    assert not any(name.startswith("cli.") for name in imported)
