"""Code provenance for a service session.

The POSIX and Windows session backends share `$AVA_HOME/run/sessions/<name>.json`,
which identifies the live process but intentionally carries no deployment state.
For daemons with the standard `/healthz` endpoint, that endpoint is the primary
source: it reports the SHA frozen by the process itself, including sessions that
predate this module. The local sidecar covers services without that endpoint and
new launches before a daemon starts serving; its identity tuple makes an old
sidecar unusable after a service respawn replaces the same session name.
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import cast

from shared.paths import ava_home, run_dir
from shared.session_record import SessionRecord

_HEALTH_TIMEOUT_S = 1.0


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


def _sidecar_sha(session: str) -> str | None:
    """The sidecar SHA for the currently identified session, else ``None``."""
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


def _health_payload(url: str) -> dict[str, object] | None:
    """Read one standard health response, treating any failure as unknown."""
    try:
        with urllib.request.urlopen(url, timeout=_HEALTH_TIMEOUT_S) as response:  # noqa: S310 -- local service URL from the roster
            if not 200 <= response.status < 300:
                return None
            payload = json.loads(response.read())
    except (OSError, TimeoutError, urllib.error.URLError, ValueError):
        return None
    return cast("dict[str, object]", payload) if isinstance(payload, dict) else None


def _health_sha(session: str, service: str, health_url: str) -> str | None:
    """The frozen SHA reported by this session's identified daemon, if any."""
    record = SessionRecord.read(_session_record_path(session))
    if record is None:
        return None
    payload = _health_payload(health_url)
    if payload is None:
        return None
    if (
        payload.get("name") != service
        or payload.get("home") != str(ava_home())
        or payload.get("pid") != record.pid
    ):
        return None
    sha = payload.get("sha")
    return sha if isinstance(sha, str) and sha else None


def launched_sha(
    session: str, *, service: str | None = None, health_url: str | None = None
) -> str | None:
    """The SHA the currently identified session actually runs, else ``None``.

    A standard health response takes precedence over the launch sidecar because
    it is spoken by the live process and survives the first deployment of this
    feature. Missing, malformed, or identity-mismatched provenance is unknown;
    the caller must not restart a healthy process on a guess.
    """
    if service is not None and health_url is not None:
        health_sha = _health_sha(session, service, health_url)
        if health_sha is not None:
            return health_sha
    return _sidecar_sha(session)
