"""Permissions helper — the desktop-automation daemon holding the machine's
desktop permissions (macOS TCC / Windows interactive-session identity).

`converge()` here dispatches by platform so callers (the converge phase, the
tests) never branch on sys.platform themselves.
"""

from __future__ import annotations

import sys


def converge() -> None:
    """Idempotent helper bring-up for this platform.

    macOS: stable cert + swift build + codesign + launchd load. Windows:
    csc build + logon-task registration + launch into the user session.
    """
    if sys.platform == "win32":
        from services.permissions_helper.windows.lifecycle import converge as _converge
    else:
        from services.permissions_helper.lifecycle import converge as _converge
    _converge()
