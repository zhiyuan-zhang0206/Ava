"""Writes an exec request envelope and dies at a chosen kill point; never imported by a production entry.

Run as ``python _envelope_crash_child.py <request-path> <os.replace|os.fsync>``; the process
exits 9 at the kill point, so a surviving envelope would mean the commit was not atomic.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from agent.graph.exec import protocol


def die(*_args: object, **_kwargs: object) -> None:
    os._exit(9)


if sys.argv[2] == "os.replace":
    os.replace = die  # type: ignore[assignment]
elif sys.argv[2] == "os.fsync":
    os.fsync = die  # type: ignore[assignment]
else:
    raise ValueError("Unknown envelope kill point")
protocol.write_request(
    Path(sys.argv[1]),
    code="x=1",
    context={"identity": {"agent_id": 7, "owns_loop": True, "actor": None}},
    timeout_s=1.0,
    state=None,
    incarnation=None,
)
