"""The helper artifact's build record: the inputs and identity it was signed from.

`lifecycle.build_and_sign` reuses a bundle only when the recorded content hash
matches every build input, and compares the recorded designated requirement with
the current one to tell whether macOS permission grants survive a rebuild. A
missing or malformed record reads as absent, which forces a rebuild.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, cast

BUILD_STATE_NAME = "build-state.json"


class BuildState(TypedDict):
    source_hash: str
    dr: str
    signed_at: str


def read_build_state(path: Path) -> BuildState | None:
    try:
        raw: object = json.loads(path.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    data = cast(dict[str, object], raw)
    try:
        source_hash = data["source_hash"]
        dr = data["dr"]
        signed_at = data["signed_at"]
    except KeyError:
        return None
    if (
        not isinstance(source_hash, str)
        or not isinstance(dr, str)
        or not isinstance(signed_at, str)
    ):
        return None
    return BuildState(source_hash=source_hash, dr=dr, signed_at=signed_at)


def write_build_state(path: Path, source_hash: str, dr: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state = BuildState(
        source_hash=source_hash,
        dr=dr,
        signed_at=datetime.now(UTC).isoformat(),
    )
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
