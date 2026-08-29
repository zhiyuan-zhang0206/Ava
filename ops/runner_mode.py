"""Fail-closed runner-mode read used by the service roster."""

from __future__ import annotations

from shared.config import settings


def runner_mode() -> str:
    """Return process mode when configuration cannot be read.

    Failing open would start the hosted runner beside process-mode agents and
    create a second claimant for inbound work. Keeping this dependency-free
    reader unable to raise makes the service gate fail closed by construction.
    """
    try:
        return str(settings.daemon.runner_mode)
    except Exception:
        return "process"
