"""Detect long-lived processes orphaned from the permissions helper."""

from __future__ import annotations

import os

from shared.process_env import inherited_process_env

_HELPER_PID_ENV = "AVA_PERMISSIONS_HELPER_PID"


def parent_chain_intact() -> bool:
    """Whether this process is still the helper's direct child.

    Processes not spawned by the helper carry no marker and are left alone.
    A malformed injected marker is treated as a broken chain rather than
    silently disabling the guard.
    """
    raw_helper_pid = inherited_process_env().get(_HELPER_PID_ENV)
    if raw_helper_pid is None:
        return True
    try:
        helper_pid = int(raw_helper_pid)
    except ValueError:
        return False
    return helper_pid > 0 and os.getppid() == helper_pid
