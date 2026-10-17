"""Disk-backed IM Bridge switch state."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import TypeAdapter

from base.paths import meiva_home

_log = logging.getLogger("services.entrypoints.im_bridge.state")


def _switch_state_path() -> Path:
    """Per-chat switch persistence — survives daemon restarts/updates."""

    path = meiva_home() / "state" / "im_bridge" / "switch_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_switch_state() -> dict[str, int]:
    """Restore {channel}:{chat_id} -> agent_id from disk; {} when absent."""

    path = _switch_state_path()
    if not path.exists():
        return {}
    return TypeAdapter(dict[str, int]).validate_json(path.read_text(encoding="utf-8"), strict=True)


def save_switch_state(state: dict[str, int]) -> None:
    """Persist atomically so a crash never leaves a half-written file."""

    path = _switch_state_path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
