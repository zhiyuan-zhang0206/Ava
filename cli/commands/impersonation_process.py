"""Observed CLI process facts, kept separate from the executor's declared name."""

import os
from typing import Any

import psutil


def process_metadata() -> dict[str, Any]:
    """Capture names and process incarnations; argv and environment may contain secrets."""
    result: dict[str, Any] = {"pid": os.getpid(), "ancestors": []}
    # env-ok: inherited provider routing context, never an executor identity assertion
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        result["codex_home"] = codex_home
    process = psutil.Process()
    for depth in range(8):
        try:
            facts = {
                "pid": process.pid,
                "name": process.name(),
                "executable": process.exe(),
                "created_at": process.create_time(),
                "parent_pid": process.ppid(),
            }
            if depth == 0:
                result.update(facts)
            else:
                result["ancestors"].append(facts)
            parent = process.parent()
            if parent is None:
                break
            process = parent
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            result["observation_error"] = type(exc).__name__
            break
    return result
