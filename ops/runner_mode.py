"""Fail-closed runner-mode read used by the service roster and the lifecycle ops."""

from __future__ import annotations

from shared.config import settings


def runner_mode() -> str:
    """Return process mode when configuration cannot be read.

    `hosted` is the settings default since 2026-09 (user ruling), so an
    ordinary fresh cluster reads hosted with no env anywhere. The EXCEPTION
    path still returns process: failing open would start the hosted runner
    beside process-mode agents and create a second claimant for inbound work.
    Keeping this dependency-free reader unable to raise makes the service gate
    fail closed by construction.
    """
    try:
        return str(settings.daemon.runner_mode)
    except Exception:
        return "process"


def is_hosted() -> bool:
    """Whether this cluster runs the hosted agent-runner.

    Same fail-closed discipline as `runner_mode`: a config-surprise (broken
    settings read) degrades to process mode — the legacy shape where every
    agent has its own process and the lifecycle ops keep forking processes —
    never to a speculative hosted mode that could double-claim inbound rows.
    """
    return runner_mode() == "hosted"
