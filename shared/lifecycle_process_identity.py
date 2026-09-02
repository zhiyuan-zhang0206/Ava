"""Exact process evidence attached to the existing durable lifecycle command.

This is an observation, not a second owner registry. Only the admitted process
captures its own identity at application; observers retain the command's fixed
generation/owner fence. Session-record cleanup cannot erase this evidence.
"""

import math
import os
from typing import Any

import psutil

from shared.session_record import SessionRecord, pid_starttime_ticks


def capture_process_identity(admitted_pid: int, machine: str) -> dict[str, object]:
    """Capture the Python runtime, never its exec child or native redirector."""
    if admitted_pid != os.getpid():
        raise RuntimeError("lifecycle application is not in the admitted Python process")
    process = psutil.Process(admitted_pid)
    return {
        "machine": machine,
        "pid": admitted_pid,
        "create_time": process.create_time(),
        "starttime": pid_starttime_ticks(admitted_pid),
    }


def target_process_ended(payload: dict[str, Any], machine: str) -> bool:
    """Positive old-process exit/reuse only; absent or unreadable evidence defers."""
    value = payload.get("target_process_identity")
    if not isinstance(value, dict):
        return False
    if set(value) != {"machine", "pid", "create_time", "starttime"}:
        return False
    pid, birth, ticks = value["pid"], value["create_time"], value["starttime"]
    if (
        value["machine"] != machine
        or type(pid) is not int
        or pid <= 0
        or type(birth) not in (int, float)
        or not math.isfinite(birth)
        or birth <= 0
        or (ticks is not None and (type(ticks) is not int or ticks <= 0))
    ):
        return False
    record = SessionRecord(pid, birth, "", "", 0, starttime=ticks)
    try:
        process = psutil.Process(record.pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return True
        if record.starttime is not None:
            matches = record.identifies(record.pid)
            return matches is False
        return process.create_time() != record.create_time
    except psutil.NoSuchProcess:
        return True
    except (psutil.AccessDenied, OSError):
        return False
