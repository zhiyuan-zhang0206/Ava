"""Path + record + protocol helpers shared by the pty session host and CLI.

The per-session namespace lives under ``$AVA_HOME/run/pty/``: one
``<name>.json`` record and one ``<name>.sock`` socket per live session. It is
deliberately NOT ``run/sessions/`` (the posixproc/winproc service-session
dir): a record scan is the session listing, and sharing the dir would make
every lister on either side regex-filter the other's records out — the
pre-2026-08 layout did exactly that and ``stop.py`` carried the filter.

The record is a ``SessionRecord`` (the SHELL is its liveness key) plus extra
keys naming the host process (``host_pid``/``host_create_time`` and optional
``host_starttime``) so a kill can reach a wedged host. ``SessionRecord.read``
ignores the extras, so any generic record reader still parses a pty record.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from shared.session_record import SessionRecord

# NOTE: `shared.paths` is imported inside each path resolver, never at module
# top: importing it builds the pydantic Settings singleton (~31 MB RSS,
# measured 2026-08-13), and the session HOST process imports this module for
# the pure record/protocol helpers while receiving its paths pre-resolved on
# argv — the host must never pay the settings import (P1 memory review).

# AF_UNIX sun_path bound: 104 bytes on macOS, 108 on Linux. A deeply nested
# $AVA_HOME (a long worktree path) pushes the natural socket path over it;
# such sessions fall back to a short tempdir path keyed by (home, name), so
# host and CLI always agree.
SUN_PATH_MAX = 100

# Terminal geometry defaults (the classic pane shape). Defined here — the
# stdlib-only module — so the host can read them without importing the screen
# module, whose import pulls pyte (kept lazy until a capture needs it).
DEFAULT_COLS = 120
DEFAULT_ROWS = 40


def pty_dir() -> Path:
    from shared.paths import run_dir

    d = run_dir() / "pty"
    d.mkdir(parents=True, exist_ok=True)
    return d


def record_path(name: str) -> Path:
    return pty_dir() / f"{name}.json"


def socket_path(name: str) -> Path:
    """The session host's unix socket; ``$AVA_HOME/run/pty/<name>.sock``."""
    from shared.paths import run_dir

    natural = pty_dir() / f"{name}.sock"
    if len(str(natural)) <= SUN_PATH_MAX:
        return natural
    digest = hashlib.sha1(f"{run_dir()}\0{name}".encode(), usedforsecurity=False).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"ava-pty-{digest}.sock"


def transcript_path(name: str) -> Path:
    """The session's byte transcript ($AVA_HOME/logs/<name>.out.log)."""
    from shared.paths import logs_dir

    return logs_dir() / f"{name}.out.log"


def host_log_path(name: str) -> Path:
    """The host process's own stdout/stderr log (diagnostics, not the
    transcript) — where the spawner points the _reparent redirect."""
    from shared.paths import logs_dir

    return logs_dir() / f"{name}.host.log"


def write_record(
    path: Path,
    record: SessionRecord,
    *,
    host_pid: int,
    host_create_time: float,
    host_starttime: int | None = None,
) -> None:
    """Persist the pty record: SessionRecord fields + the host identity keys.

    Same atomic temp-file + rename discipline as ``SessionRecord.write`` (a
    concurrent reader must never see a truncated record).
    """
    payload = asdict(record) | {
        "host_pid": host_pid,
        "host_create_time": host_create_time,
        "host_starttime": host_starttime,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)


def host_identity(path: Path) -> tuple[int, float] | None:
    """(host_pid, host_create_time) from a pty record, or None if absent."""
    try:
        raw = json.loads(path.read_text())
    except (ValueError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    data = cast("dict[str, Any]", raw)
    if "host_pid" not in data:
        return None
    return int(data["host_pid"]), float(data.get("host_create_time", 0.0))


def host_starttime(path: Path) -> int | None:
    """Optional Linux clock-tick identity for a pty record's host process."""
    try:
        raw = json.loads(path.read_text())
    except (ValueError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    value = cast("dict[str, Any]", raw).get("host_starttime")
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def ok(data: dict[str, object] | None = None) -> dict[str, object]:
    return {"ok": True, "code": 0, "data": data, "error": None}


def err(code: int, message: str) -> dict[str, object]:
    return {"ok": False, "code": code, "data": None, "error": message}
