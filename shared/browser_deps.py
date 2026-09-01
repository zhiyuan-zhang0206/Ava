"""Settings-free browser dependency detection, repair, and operator guidance.

This is the settings-free half of the browser-deps contract, used by
``cli.enroll`` on a fresh host before Settings can be built and by converge's
browser step. It imports only the standard library plus settings-free probes
from ``shared.platform_probes``; it must never import ``shared.config`` or a
``cli`` module.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from shared.platform_probes import NPX_INCAPABILITY_REASON, browser_deps_incapability


def node_install_command() -> str:
    """Copy-pasteable per-platform command that installs Node.js (npx)."""
    if sys.platform == "darwin":
        return "brew install node  (or: brew install node@22 && brew link --force node@22)"
    if sys.platform.startswith("linux"):
        return "curl -fsSL https://deb.nodesource.com/setup_22.x | sudo bash - && sudo apt-get install -y nodejs"
    if sys.platform == "win32":
        return "winget install OpenJS.NodeJS.LTS"
    return "install Node.js >= 20.9 (see https://nodejs.org)"


def install_nodejs() -> bool:
    """Best-effort Node.js install; return True only when npx is on PATH afterwards."""
    if shutil.which("npx") is not None:
        return True
    if sys.platform == "win32":
        return False
    if sys.platform == "darwin" or sys.platform.startswith("linux"):
        provisioner = Path(__file__).resolve().parents[1] / "scripts" / "provision" / "node.sh"
        try:
            subprocess.run(  # noqa: S603 — fixed repo-owned provisioner path
                ["bash", str(provisioner)],
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
    return shutil.which("npx") is not None


def ensure_browser_deps() -> str | None:
    """Detect and repair browser dependencies, returning a remaining reason if any."""
    reason = browser_deps_incapability()
    if reason is None or reason != NPX_INCAPABILITY_REASON:
        return reason
    install_nodejs()
    return browser_deps_incapability()


def browser_deps_notice(reason: str) -> str:
    """Return the informational display-less-host outcome for operator output."""
    return (
        f"ava-browser is not applicable on this host: {reason} — a headed browser cannot run "
        "here; nothing to install. The service stays skipped by design."
    )


def browser_deps_warning(reason: str) -> str:
    """Return a prominent repair warning for a browser dependency gap."""
    if reason == NPX_INCAPABILITY_REASON:
        repair = (
            "| Install Node.js (npx):                                         |\n"
            f"|   {node_install_command()}\n"
        )
    elif reason.startswith("no Chrome"):
        repair = "| Install Google Chrome or set AVA_CHROME_BINARY.                |\n"
    else:
        repair = (
            "| A headed browser cannot run without a display; nothing to      |\n"
            "| install for ava-browser on this host.                          |\n"
        )
    return (
        "+----------------------------------------------------------------+\n"
        "| WARNING: ava-browser dependencies are incomplete               |\n"
        "+----------------------------------------------------------------+\n"
        f"| Reason: {reason}\n"
        "| ava-browser will not run on this host until this is fixed.     |\n"
        "+----------------------------------------------------------------+\n"
        f"{repair}"
        "| Display, Chrome, and npx are all required; the first missing  |\n"
        "| requirement is shown above (checked in that order).            |\n"
        "+----------------------------------------------------------------+"
    )
