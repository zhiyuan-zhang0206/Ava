"""The one-time browser login link `ava start` prints once the UI is ready.

The link carries a short-lived, single-use login ticket in the URL *fragment*
(`<ui>/#ticket=<ticket>`): a fragment never reaches a server log or a Referer.
The ticket comes from the gateway's `POST /api/auth/login-ticket`, called with
the home's human cluster secret, and the browser redeems it for the ordinary
session cookie (gateway/http/auth/docs/web-sessions.ava.okf.md).

The link is for a person at a terminal: a start whose output is not a terminal
(a supervisor, a rollout child, a pipe) mints nothing, so no ticket lands in a
log. A unit without the human secret (a pure agent-runner) has no UI to link.
"""

from __future__ import annotations

import sys
from collections.abc import Collection
from urllib.parse import quote

_TIMEOUT_S = 10.0


def ui_base(sessions: Collection[str]) -> str | None:
    """Where a browser reaches the UI among the launched `sessions`, or None if none.

    Gate owns the public entry (the HTTPS browser origin when one is configured,
    else the entry URL); without Gate the Next.js app is reached directly on its
    reserved port.
    """
    from base.config import settings

    if "gate" in sessions:
        return (
            settings.gateway.browser_origin or settings.services.frontend_healthcheck_url
        ).rstrip("/")
    if "frontend" in sessions and settings.services.app_port is not None:
        return f"http://localhost:{settings.services.app_port}"
    return None


def at_terminal() -> bool:
    return sys.stdout.isatty()


def _issue_ticket(secret: str) -> str:
    """A fresh login ticket from this home's gateway.

    Raises:
        httpx.HTTPError: the gateway is unreachable or refused the request.
        ValueError: the gateway answered without a ticket.
    """
    from base.cluster.machine import gateway_api_base
    from base.cluster.machine.ambient import gateway_access_from_settings
    from base.host.net.auth import bearer_header
    from base.host.net.http_dial import post as dial_post

    access = gateway_access_from_settings()  # ambient: C
    response = dial_post(
        f"{gateway_api_base(access.gateway_url)}/api/auth/login-ticket",
        headers=bearer_header(secret),
        timeout=_TIMEOUT_S,
    )
    response.raise_for_status()
    ticket = response.json().get("ticket")
    if not isinstance(ticket, str) or not ticket:
        raise ValueError("the gateway answered without a login ticket")
    return ticket


def print_login_link(sessions: Collection[str]) -> None:
    """Print the one-time UI login link, or say why there is none.

    Inapplicable cases (no UI launched, no human secret, output not a terminal)
    print nothing or one line; a gateway failure is reported on stderr and never
    fails the start, because the web login password stays usable.
    """
    import httpx

    from base.config import settings

    base = ui_base(sessions)
    secret = settings.data_plane.cluster_secret
    if base is None or not secret:
        return
    if not at_terminal():
        print("\n→ one-time UI login link skipped (output is not a terminal)")
        return
    try:
        ticket = _issue_ticket(secret)
    except (httpx.HTTPError, ValueError) as exc:
        print(f"\n  ! could not mint a one-time UI login link: {exc}", file=sys.stderr)
        return
    print("\n→ open the UI (one-time login link, expires in two minutes):")
    print(f"  {base}/#ticket={quote(ticket, safe='.-_')}")
