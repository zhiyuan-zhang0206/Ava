"""Child process: hold the handoff snapshot writes of one generated scenario until released.

Usage: `child_budget_writer.py ROOT SCENARIO_CODE`; SCENARIO_CODE is the generated fixture program.
"""

import os
import sys
import time
from pathlib import Path
from typing import Any

import meiva
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity

root = Path(sys.argv[1])
publications = [0]


def hold_publication(previous: str) -> None:
    publications[0] += 1
    publication = publications[0]
    (root / f"previous-{publication}").write_text(previous)
    (root / f"ready-{publication}").touch()
    deadline = time.monotonic() + 10
    while not (root / f"release-{publication}").exists():
        if time.monotonic() > deadline:
            raise TimeoutError("Reader did not release publication")
        time.sleep(0.01)


original_open = Path.open


def held_open(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
    is_snapshot = path.name == "1.json" and mode == "w"
    previous = (root / "1.json").read_text() if is_snapshot else ""
    stream = original_open(path, mode, *args, **kwargs)
    if is_snapshot:
        hold_publication(previous)
    return stream


original_fdopen = os.fdopen


def held_fdopen(fd: int, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
    previous = (root / "1.json").read_text() if mode == "w" else ""
    stream = original_fdopen(fd, mode, *args, **kwargs)
    if mode == "w":
        hold_publication(previous)
    return stream


def spawn(**_kwargs: object) -> int:
    return 2


def send_message(*_args: object, **_kwargs: object) -> None:
    return None


Path.open = held_open  # pyright: ignore[reportAttributeAccessIssue]
os.fdopen = held_fdopen  # pyright: ignore[reportAttributeAccessIssue]
meiva.context = MeivaContext(identity=AgentIdentity(agent_id=1, owns_loop=False))
meiva.agents.spawn = spawn
meiva.agents.send_message = send_message
exec(sys.argv[2])
