"""Single source of truth for host platform probes: Chrome binary, display, AF_UNIX.

The agent-runner has three pre-launch facts the rest of the system repeatedly
needs to know about the local machine:

- Where is a launchable Chrome binary (explicit override, else platform
  default)?
- Does this machine have a window server a headed browser can draw on?
- Does it have AF_UNIX sockets (the browser-mcp daemon's transport)?

These probes lived in three places (the browser daemon, the MCP config loader,
and the host-config validators) and had already drifted apart. They now live
here, in ``shared`` (the lowest import layer), so the browser daemon
(``services``), the MCP loader (``ava``), and the host-config validators
(``shared``) all import the same logic.

``shared.config.settings`` is imported lazily inside the function that needs it
to avoid the ``shared.config`` circular import (same pattern as
``shared.runtime_config``).
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
from pathlib import Path

_MACOS_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
_LINUX_CHROME_NAMES = ("google-chrome", "google-chrome-stable")
_WINDOWS_CHROME_PATHS = (
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    str(Path(os.environ.get("LOCALAPPDATA", "")) / "Google\\Chrome\\Application\\chrome.exe"),
)
NPX_INCAPABILITY_REASON = "no npx (install Node.js for chrome-devtools-mcp)"


def _platform_chrome_binary() -> str | None:
    """Resolve a platform-default Chrome binary without reading Settings."""
    if sys.platform == "darwin":
        return _MACOS_CHROME if Path(_MACOS_CHROME).exists() else None
    if sys.platform.startswith("linux"):
        for name in _LINUX_CHROME_NAMES:
            found = shutil.which(name)
            if found:
                return found
        return None
    if sys.platform == "win32":
        for path in _WINDOWS_CHROME_PATHS:
            if Path(path).exists():
                return path
        # Also try PATH lookup (user may have added Chrome dir to PATH)
        for name in ("chrome.exe", "chrome"):
            found = shutil.which(name)
            if found:
                return found
        return None
    return None  # other: unsupported


def resolve_chrome_binary() -> str | None:
    """Resolve a Chrome binary path for this host, or None if none is found.

    Resolution order:
    - explicit ``settings.services.chrome_binary`` override -> returned **as-is** (no
      existence check; a caller that execs it fails loudly at exec time if the
      path is wrong, which is the intended fail-fast for the browser daemon).
    - platform-default Chrome binary -> resolved by ``_platform_chrome_binary()``.
    """
    from shared.config import settings

    if settings.services.chrome_binary:
        return settings.services.chrome_binary
    return _platform_chrome_binary()


def default_chrome_user_data_dir() -> Path | None:
    """The user's *daily* Chrome user-data dir for this host, or None.

    This is the top-level Chrome data dir (it holds ``Default/``, ``Profile N/``,
    and ``Local State``), NOT a single profile — copying it whole carries every
    signed-in profile plus the cookie jars and saved passwords. It is the source
    the install/first-start prompt copies from when the operator opts to seed the
    agent's dedicated profile with their own logged-in Chrome state.

    Returns None when the platform has no known location (Windows/other) or the
    dir simply does not exist (Chrome never installed / never launched) — the
    caller degrades to a fresh empty profile, which is environment detection, not
    a swallowed error.
    """
    if sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    elif sys.platform.startswith("linux"):
        path = Path.home() / ".config" / "google-chrome"
    else:
        return None
    return path if path.is_dir() else None


def display_available() -> bool:
    """Return True if a window server a headed browser can use is reachable.

    macOS: always True. Known limitation: a headless macOS server (SSH-only, no
    logged-in desktop) cannot be cheaply distinguished from a headed one via
    env-vars alone, so macOS is treated as always having a display.

    Linux: ``$DISPLAY`` (X11) or ``$WAYLAND_DISPLAY`` must be set and non-empty
    (WSL without WSLg / headless servers have neither).

    Any other platform: False (fail-safe -- never assume a display on a host we
    cannot confirm has one).
    """
    if sys.platform == "darwin":
        return True
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if sys.platform == "win32":
        # Windows: assume display is available (similar to macOS —
        # headless Windows Server is rare and can use AVA_CHROME_BINARY override)
        return True
    return False


def browser_incapability() -> str | None:
    """Return why this host cannot run a headed Chrome, or None if it can.

    The single source of truth for the three-prong capability check (display +
    Chrome binary + npx), evaluated in that order. The returned string is
    operator-facing -- it surfaces in ``ava status``, the ``ava start`` /
    watchdog / warmup skip logs, and the RuntimeError the browser daemon raises
    at launch -- so each reason names the fix, not just the missing prong.

    ``browser_capable()`` (the bool gate) and
    ``services.browser.daemon.assert_browser_capable()`` (the raising preflight)
    both derive from this, so the capability logic lives in exactly one place.
    """
    if not display_available():
        return "no display (WSL without WSLg / headless server)"
    if resolve_chrome_binary() is None:
        return "no Chrome (install it or set AVA_CHROME_BINARY)"
    if shutil.which("npx") is None:
        return NPX_INCAPABILITY_REASON
    return None


def browser_deps_incapability() -> str | None:
    """Settings-free capability check for install/enroll-time use.

    Same three prongs, same order, same reason strings as
    ``browser_incapability()``, minus the AVA_CHROME_BINARY override — enroll
    runs on a fresh host before Settings can be built, so it cannot read the
    override. Callers that have Settings use ``browser_incapability()``.
    """
    if not display_available():
        return "no display (WSL without WSLg / headless server)"
    if _platform_chrome_binary() is None:
        return "no Chrome (install it or set AVA_CHROME_BINARY)"
    if shutil.which("npx") is None:
        return NPX_INCAPABILITY_REASON
    return None


def browser_capable() -> bool:
    """Return True if this host can run a headed Chrome (display + binary + npx).

    Thin bool wrapper over ``browser_incapability()`` for the gates that only
    need yes/no (the service roster's start decision, the host-config validator).
    Callers that want to show or log *why* call ``browser_incapability()``.
    """
    return browser_incapability() is None


def unix_sockets_available() -> bool:
    """Return True if this host has AF_UNIX stream sockets.

    True on POSIX, False on Windows — where the constant is simply absent from
    the ``socket`` module, so naming it raises ``AttributeError`` rather than
    failing a connect. Probed by attribute presence, not by ``sys.platform``,
    because the attribute IS the thing every caller actually needs.
    """
    return hasattr(socket, "AF_UNIX")


def browser_mcp_incapability() -> str | None:
    """Return why this host cannot run the shared browser-mcp daemon, or None.

    browser-mcp fronts the headed browser, so it needs every prong
    ``browser_incapability()`` checks PLUS an AF_UNIX transport: the daemon
    listens with ``asyncio.start_unix_server`` and each agent's wrapper dials it
    with ``asyncio.open_unix_connection`` (``services/browser/mcp_daemon.py``,
    ``services/browser/mcp_wrapper.py``). Windows has none of the three, so the
    daemon dies at startup and its healthcheck cannot even probe. The AF_UNIX
    prong is checked FIRST: on a host that lacks it, no amount of Chrome or npx
    makes the service runnable, so that is the reason worth showing.

    Porting the transport to a named pipe / loopback socket is future work
    (``future/infra/windows-browser-mcp.md``); until then this is what
    keeps browser-mcp out of a Windows agent-runner's start roster.
    """
    if not unix_sockets_available():
        return "no AF_UNIX sockets (browser-mcp's transport is POSIX-only)"
    return browser_incapability()


# csc.exe ships with the .NET Framework present on every Windows 10/11 install;
# the M1 prototype verified this exact path on the fleet Windows box. Shared
# between the capability probe (shared layer) and the Windows helper build
# (services layer) so the two can never disagree about what "capable" means.
WINDOWS_CSC_CANDIDATES = (
    r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
    r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe",
)


def permissions_helper_incapability() -> str | None:
    """Return why this host cannot run the permissions helper, or None if it can.

    The single source of truth for the capability check. macOS needs the Swift
    toolchain + codesign to build and sign the launchd helper plus a window
    server; Windows needs only the .NET Framework csc.exe (the C# helper's
    session capability is checked at runtime by the helper itself, not here --
    converge runs in Session 0 and must not fail the host for it). Other
    platforms get no helper. The returned string is operator-facing -- it
    surfaces in the converge preflight and the start skip logs.

    ``permissions_helper_capable()`` (the bool gate) derives from this, so the
    capability logic lives in exactly one place.
    """
    if sys.platform == "darwin":
        if shutil.which("swiftc") is None:
            return "no swiftc (install the Xcode command line tools)"
        if shutil.which("codesign") is None:
            return "no codesign (install the Xcode command line tools)"
        if not display_available():
            return "no display (the permissions helper drives an on-screen desktop)"
        return None
    if sys.platform == "win32":
        if not any(Path(c).exists() for c in WINDOWS_CSC_CANDIDATES):
            return "no csc.exe (install .NET Framework or a dotnet SDK)"
        return None
    return "macOS or Windows only (permissions helper drives the desktop)"


def permissions_helper_capable() -> bool:
    """Return True if this host can build, sign, and run the permissions helper."""
    return permissions_helper_incapability() is None
