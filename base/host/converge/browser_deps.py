"""Settings-free browser dependency operator guidance.

This is the settings-free half of the browser-deps contract, used by converge's
browser step. It imports only the standard library plus settings-free probes
from ``base.host.system.probes``; it must never import ``base.config`` or a
``cli`` module.
"""

from __future__ import annotations

from base.host.system.hostport import Host
from base.host.system.probes import NPX_INCAPABILITY_REASON


def node_install_command(*, host: Host) -> str:
    """Copy-pasteable per-platform command that installs Node.js (npx)."""
    if host.platform.system == "darwin":
        return "brew install node  (or: brew install node@22 && brew link --force node@22)"
    return "curl -fsSL https://deb.nodesource.com/setup_22.x | sudo bash - && sudo apt-get install -y nodejs"


def browser_deps_notice(reason: str) -> str:
    """Return the informational display-less-host outcome for operator output."""
    return (
        f"meiva-browser is not applicable on this host: {reason} — a headed browser cannot run "
        "here; nothing to install. The service stays skipped by design."
    )


def browser_deps_warning(reason: str, *, host: Host) -> str:
    """Return a prominent repair warning for a browser dependency gap."""
    if reason == NPX_INCAPABILITY_REASON:
        repair = (
            "| Install Node.js (npx):                                         |\n"
            f"|   {node_install_command(host=host)}\n"
        )
    elif reason.startswith("no Chrome"):
        repair = (
            "| Install Google Chrome (after the first start, MEIVA_CHROME_BINARY|\n"
            "| can point at an existing binary).                              |\n"
        )
    else:
        repair = (
            "| A headed browser cannot run without a display; nothing to      |\n"
            "| install for meiva-browser on this host.                          |\n"
        )
    return (
        "+----------------------------------------------------------------+\n"
        "| WARNING: meiva-browser dependencies are incomplete               |\n"
        "+----------------------------------------------------------------+\n"
        f"| Reason: {reason}\n"
        "| meiva-browser will not run on this host until this is fixed.     |\n"
        "+----------------------------------------------------------------+\n"
        f"{repair}"
        "| Display, Chrome, and npx are all required; the first missing  |\n"
        "| requirement is shown above (checked in that order).            |\n"
        "+----------------------------------------------------------------+"
    )
