"""JSON persistence for idle-shell reminder state."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from .engine import SessionState

_VERSION = 1


def load_state(path: Path) -> dict[str, SessionState]:
    """Load the versioned state file; a missing file is an empty first boot."""
    if not path.exists():
        return {}
    raw = cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))
    if raw["version"] != _VERSION:
        raise ValueError(f"unsupported idle-shell reminder state version: {raw['version']!r}")
    sessions = cast("dict[str, dict[str, Any]]", raw["sessions"])
    return {
        name: SessionState(
            owner=int(values["owner"]),
            idle_start=None if values["idle_start"] is None else float(values["idle_start"]),
            level=int(values["level"]),
            exempt=bool(values["exempt"]),
            last_reminded_at=None
            if values["last_reminded_at"] is None
            else float(values["last_reminded_at"]),
            last_reminder_inbound_id=None
            if values["last_reminder_inbound_id"] is None
            else int(values["last_reminder_inbound_id"]),
        )
        for name, values in sessions.items()
    }


def save_state(path: Path, state: dict[str, SessionState]) -> None:
    """Atomically replace the complete state snapshot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _VERSION,
        "sessions": {name: asdict(session) for name, session in sorted(state.items())},
    }
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
