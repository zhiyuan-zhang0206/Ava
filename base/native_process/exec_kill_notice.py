"""The host's note, left beside an exec run's result file, on why it killed that exec.

The agent host's memory guard writes it before killing the exec's session leader;
the run that owns the exec reads (and consumes) it once the root has exited, so
the agent's result names the cause instead of a missing result envelope. Both
sides know only the result path, so the note travels as a file.
"""

from __future__ import annotations

import json
from pathlib import Path


def notice_path(result_path: Path) -> Path:
    """Where the host records why it killed the exec writing `result_path`."""
    return result_path.with_name(result_path.name + ".host-kill")


def write_notice(result_path: Path, notice: str) -> None:
    notice_path(result_path).write_text(json.dumps({"notice": notice}), encoding="utf-8")


def read_notice(result_path: Path) -> str | None:
    """The host's kill reason for this run, consumed so it never outlives the run."""
    path = notice_path(result_path)
    try:
        text = json.loads(path.read_text(encoding="utf-8"))["notice"]
    except FileNotFoundError:
        return None
    path.unlink(missing_ok=True)
    return str(text)
