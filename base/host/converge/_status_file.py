"""Shared JSON and file framing for converge statuses."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path


def to_json(state: str, diagnostic: str) -> str:
    return json.dumps({"state": state, "diagnostic": diagnostic})


def from_json[State, Status](
    text: str, state_type: Callable[[str], State], status_type: Callable[[State, str], Status]
) -> Status:
    data = json.loads(text)
    return status_type(state_type(data["state"]), data["diagnostic"])


def from_file[Status](path: Path, decode: Callable[[str], Status]) -> Status | None:
    if not path.exists():
        return None
    try:
        return decode(path.read_text())
    except Exception:
        # A truncated write or obsolete shape is replaced by the next converge.
        return None


def write_status(path: Path, text: str) -> None:
    path.write_text(text)
