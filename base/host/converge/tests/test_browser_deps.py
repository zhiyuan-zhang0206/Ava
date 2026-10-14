"""Settings-free browser dependency operator guidance."""

from __future__ import annotations

import ast
import inspect

import pytest

from base.host.converge import browser_deps
from base.host.system import probes
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import DARWIN_ARM64, LINUX_X86, fake_host
from base.native_process.os_platform import Platform


def _install_node_here(*, host: Host) -> str:
    return "install-node-here"


@pytest.mark.parametrize(
    ("platform", "expected_tool"),
    [
        (DARWIN_ARM64, "brew"),
        (LINUX_X86, "apt-get"),
    ],
)
def test_node_install_command_names_the_platform_installer(
    platform: Platform, expected_tool: str
) -> None:
    """A missing npx warning gives this host a copy-pasteable repair command."""
    command = browser_deps.node_install_command(host=fake_host(platform=platform))
    assert command
    assert expected_tool in command


def test_node_install_command_explains_the_macos_keg_only_fallback() -> None:
    """Mac operators get a command that leaves npx on PATH after install."""
    assert browser_deps.node_install_command(host=fake_host(platform=DARWIN_ARM64)) == (
        "brew install node  (or: brew install node@22 && brew link --force node@22)"
    )


def test_browser_deps_warning_names_the_npx_reason_and_platform_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The warning tells the operator both why ava-browser is skipped and how to fix it."""
    monkeypatch.setattr(browser_deps, "node_install_command", _install_node_here)
    warning = browser_deps.browser_deps_warning(probes.NPX_INCAPABILITY_REASON, host=fake_host())
    assert probes.NPX_INCAPABILITY_REASON in warning
    assert "install-node-here" in warning
    assert "ava-browser will not run on this host until this is fixed" in warning


def test_browser_deps_warning_explains_that_a_missing_display_needs_no_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A display-less host must not receive a Node repair instruction."""
    monkeypatch.setattr(browser_deps, "node_install_command", _install_node_here)
    warning = browser_deps.browser_deps_warning("no display (headless server)", host=fake_host())
    assert "install-node-here" not in warning
    assert "nothing to" in warning
    assert "install for ava-browser" in warning


def test_browser_deps_warning_explains_how_to_repair_missing_chrome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chrome is the missing prong, so its repair—not Node's—must lead the box."""
    monkeypatch.setattr(browser_deps, "node_install_command", _install_node_here)
    warning = browser_deps.browser_deps_warning(
        "no Chrome (install it or set AVA_CHROME_BINARY)", host=fake_host()
    )
    assert "install-node-here" not in warning
    assert "Install Google Chrome" in warning
    assert "AVA_CHROME_BINARY" in warning


def test_browser_deps_notice_marks_a_displayless_host_not_applicable() -> None:
    """Fresh enrollment names the deliberate browser skip instead of a repair alarm."""
    reason = "no display (headless server)"
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
    assert "base.config" not in imported
    assert not any(name.startswith("cli.") for name in imported)
