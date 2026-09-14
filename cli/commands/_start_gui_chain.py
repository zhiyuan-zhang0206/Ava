"""The macOS GUI-chain warning for `ava start` (task #3346).

Service sessions inherit the launchd management domain of the chain that spawns
them, and a chain outside the GUI login session cannot read the login Keychain
— the state that wedges the headed browser until something re-homes it. `ava
start` detects that chain before launching anything and names the remedy. The
warning is advisory (it never reroutes the start) and stays silent whenever a
re-home is impossible (no GUI login of this account, an unknown domain) or
unneeded (Aqua, other platforms, hosts without the agent-runner role).
"""

from __future__ import annotations

import os
import sys

from shared.machine import MachineRoles
from shared.os_autostart import gui_domain_kickstart_command
from shared.platform import IS_MACOS
from shared.platform_probes import gui_login_user, gui_session_domain


def _current_account_name() -> str | None:
    """This process's login account name, or None when unavailable."""
    if not IS_MACOS:
        return None
    import pwd  # POSIX-only module, reached only on the macOS path

    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:  # pragma: no cover - a uid without a passwd entry
        return None


def _rehomeable_domain(roles: MachineRoles) -> str | None:
    """This start's non-Aqua launchd domain when a re-home through the GUI-domain
    job is possible, else None.

    One gate for both the warning and the handover (task #3348): macOS, the
    agent-runner role, an answer other than Aqua (None is not evidence), and a
    GUI login owned by this account (else there is no session to re-home into).
    """
    if not IS_MACOS or "agent-runner" not in roles:
        return None
    domain = gui_session_domain()
    if domain is None or domain == "Aqua":
        return None
    account = _current_account_name()
    console_user = gui_login_user()
    if account is None or console_user is None or console_user != account:
        return None
    return domain


def _warn_when_chain_outside_gui_session(roles: MachineRoles) -> None:
    """Warn when this start's chain is outside the macOS GUI login session."""
    domain = _rehomeable_domain(roles)
    if domain is None:
        return
    print(
        "  ! this start chain is outside the macOS GUI login session "
        f"(launchd managername: {domain}).\n"
        "    Sessions started from here inherit that domain and cannot read the login\n"
        "    Keychain — the headed browser wedges until a GUI-domain relaunch repairs it.\n"
        "    Re-run the start through the GUI domain instead — a Terminal in the login\n"
        "    session, or:\n"
        f"      {gui_domain_kickstart_command()}",
        file=sys.stderr,
    )
