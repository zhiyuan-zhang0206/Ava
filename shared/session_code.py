"""Code provenance for a service session.

The POSIX and Windows session backends share `$AVA_HOME/run/sessions/<name>.json`,
which identifies the live process but intentionally carries no deployment state.
This sidecar records the checkout SHA a launcher used for one such identity. Its
identity tuple makes an old sidecar unusable after a service respawn replaces the
same session name, rather than mistaking the replacement for stale code.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import cast

from shared.paths import run_dir
from shared.session_record import SessionRecord


def _session_record_path(session: str) -> Path:
    return run_dir() / "sessions" / f"{session}.json"


def _code_record_path(session: str) -> Path:
    return run_dir() / "session-code" / f"{session}.json"


def _identity(record: SessionRecord) -> dict[str, int | float | None]:
    return {
        "pid": record.pid,
        "started_at": record.started_at,
        "starttime": record.starttime,
    }


def record_launch(session: str, sha: str) -> None:
    """Remember ``sha`` for the live session identity, best-effort.

    The service is already running when this is called. A record failure must not
    turn that successful launch into a failed start; without provenance the next
    start conservatively leaves the session alone.
    """
    record = SessionRecord.read(_session_record_path(session))
    if record is None:
        return
    path = _code_record_path(session)
    tmp: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "w") as handle:
            json.dump({"sha": sha, **_identity(record)}, handle)
        Path(tmp).replace(path)
    except OSError:
        if tmp is not None:
            Path(tmp).unlink(missing_ok=True)


def launched_sha(session: str) -> str | None:
    """The SHA recorded for the currently identified session, else ``None``.

    Missing, malformed, or identity-mismatched provenance is intentionally
    unknown. The caller must not restart a healthy process on a guess.
    """
    record = SessionRecord.read(_session_record_path(session))
    if record is None:
        return None
    try:
        data = json.loads(_code_record_path(session).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    payload = cast("dict[str, object]", data)
    if any(payload.get(key) != value for key, value in _identity(record).items()):
        return None
    sha = payload.get("sha")
    return sha if isinstance(sha, str) and sha else None
