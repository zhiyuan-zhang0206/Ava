"""Settings-free browser dependency detection, repair, and operator guidance."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared import browser_deps

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NPX_REASON = "no npx (install Node.js for chrome-devtools-mcp)"


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


def test_install_nodejs_short_circuits_when_npx_is_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """An already-capable host never reruns the provisioner."""
    monkeypatch.setattr(browser_deps.shutil, "which", lambda _name: "/usr/bin/npx")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(
        browser_deps.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("provisioner ran despite npx on PATH"),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
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
        lambda _name: "/usr/bin/npx" if npx_available else None,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
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
    monkeypatch.setattr(browser_deps.shutil, "which", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(
        browser_deps.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Windows must not run a silent installer"),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
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
    reasons = iter([_NPX_REASON, None])
    monkeypatch.setattr(browser_deps, "browser_deps_incapability", lambda: next(reasons))
    monkeypatch.setattr(browser_deps, "install_nodejs", lambda: True)
    assert browser_deps.ensure_browser_deps() is None


def test_ensure_browser_deps_returns_npx_reason_when_repair_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed best-effort repair keeps the actionable missing-npx reason."""
    monkeypatch.setattr(browser_deps, "browser_deps_incapability", lambda: _NPX_REASON)
    monkeypatch.setattr(browser_deps, "install_nodejs", lambda: False)
    assert browser_deps.ensure_browser_deps() == _NPX_REASON


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


def test_browser_deps_warning_names_the_reason_and_platform_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The warning tells the operator both why ava-browser is skipped and how to fix it."""
    monkeypatch.setattr(browser_deps, "node_install_command", lambda: "install-node-here")
    warning = browser_deps.browser_deps_warning(_NPX_REASON)
    assert _NPX_REASON in warning
    assert "install-node-here" in warning
    assert "ava-browser will not run on this host until this is fixed" in warning


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
