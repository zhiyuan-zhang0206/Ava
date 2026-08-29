"""Fail-closed runner-mode read used by the service roster and the lifecycle ops."""

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


def is_hosted() -> bool:
    """Whether this cluster runs the hosted agent-runner.

    Same fail-closed discipline as `runner_mode`: any config surprise reads as
    process mode, and process mode is where the lifecycle ops keep forking
    processes. Hosted is only ever entered on a deliberate, readable
    `AVA_RUNNER_MODE=hosted`.
    """
    return runner_mode() == "hosted"
