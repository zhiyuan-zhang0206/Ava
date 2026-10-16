from __future__ import annotations

import os
from pathlib import Path

from base.host.system.hostport import Host

MACOS_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
_LINUX_CHROME_NAMES = ("google-chrome", "google-chrome-stable")
NPX_INCAPABILITY_REASON = "no npx (install Node.js for chrome-devtools-mcp)"


def platform_chrome_binary(*, host: Host) -> str | None:
    """Resolve a platform-default Chrome binary without reading Settings."""
    if host.platform.system == "darwin":
        return MACOS_CHROME if Path(MACOS_CHROME).exists() else None
    for name in _LINUX_CHROME_NAMES:
        found = host.which(name)
        if found:
            return found
    return None


def resolve_chrome_binary(configured_chrome: str | None, *, host: Host) -> str | None:
    """Resolve a Chrome binary path for this host, or None if none is found.

    Resolution order:
    - explicit ``configured_chrome`` override (``settings.services.chrome_binary``) -> returned **as-is** (no
      existence check; a caller that execs it fails loudly at exec time if the
      path is wrong, which is the intended fail-fast for the browser daemon).
    - platform-default Chrome binary -> resolved by ``platform_chrome_binary()``.
    """
    if configured_chrome:
        return configured_chrome
    return platform_chrome_binary(host=host)


def default_chrome_user_data_dir(*, host: Host) -> Path | None:
    """The user's *daily* Chrome user-data dir for this host, or None.

    This is the top-level Chrome data dir (it holds ``Default/``, ``Profile N/``,
    and ``Local State``), NOT a single profile — copying it whole carries every
    signed-in profile plus the cookie jars and saved passwords. It is the source
    used by the browser profile's explicit import of the operator's logged-in
    Chrome state.

    Returns None when the dir does not exist (Chrome never installed / never
    launched) — the caller degrades to a fresh empty profile, which is environment detection, not
    a swallowed error.
    """
    if host.platform.system == "darwin":
        path = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    else:
        path = Path.home() / ".config" / "google-chrome"
    return path if path.is_dir() else None


def display_available(*, host: Host) -> bool:
    """Return True if a window server a headed browser can use is reachable.

    macOS: always True. Known limitation: a headless macOS server (SSH-only, no
    logged-in desktop) cannot be cheaply distinguished from a headed one via
    env-vars alone, so macOS is treated as always having a display.

    Linux: ``$DISPLAY`` (X11) or ``$WAYLAND_DISPLAY`` must be set and non-empty
    (a headless server has neither).

    ``Platform`` admits only macOS and Linux, so there is no other case.
    """
    if host.platform.system == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def browser_incapability(configured_chrome: str | None, *, host: Host) -> str | None:
    """Return why this host cannot run a headed Chrome, or None if it can.

    The single source of truth for the three-prong capability check (display +
    Chrome binary + npx), evaluated in that order. The returned string is
    operator-facing -- it surfaces in ``ava status``, the ``ava start`` /
    watchdog / warmup skip logs, and the RuntimeError the browser daemon raises
    at launch -- so each reason names the fix, not just the missing prong.

    ``browser_capable()`` (the bool gate) and
    ``services.desktop.browser.daemon.assert_browser_capable()`` (the raising preflight)
    both derive from this, so the capability logic lives in exactly one place.
    """
    if not display_available(host=host):
        return "no display (headless server)"
    if resolve_chrome_binary(configured_chrome, host=host) is None:
        return "no Chrome (install it or set AVA_CHROME_BINARY)"
    if host.which("npx") is None:
        return NPX_INCAPABILITY_REASON
    return None


def browser_capable(configured_chrome: str | None, *, host: Host) -> bool:
    """Return True if this host can run a headed Chrome (display + binary + npx).

    Thin bool wrapper over ``browser_incapability()`` for the gates that only
    need yes/no (the service roster's start decision, the host-config validator).
    Callers that want to show or log *why* call ``browser_incapability()``.
    """
    return browser_incapability(configured_chrome, host=host) is None


def permissions_helper_incapability(*, host: Host) -> str | None:
    if host.platform.system == "darwin":
        if host.which("swiftc") is None:
            return "no swiftc (install the Xcode command line tools)"
        if host.which("codesign") is None:
            return "no codesign (install the Xcode command line tools)"
        if not display_available(host=host):
            return "no display (the permissions helper drives an on-screen desktop)"
        return None
    return "macOS only (permissions helper drives the desktop)"
