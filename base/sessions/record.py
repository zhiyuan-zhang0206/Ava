"""On-disk session record for the POSIX process supervisor.

`base.sessions.posixproc` persists a launched background session as JSON under
`$AVA_HOME/run/sessions/<name>.json`.

On Linux, WSL2 may step `/proc/stat`'s `btime`, which makes psutil's epoch
`create_time()` drift for a still-live pid. `starttime` records `/proc/<pid>/stat`
field 22 instead: clock ticks since boot are monotonic and therefore the stable
process identity when available; `create_time` remains for legacy
records.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from base.native_process import pid_starttime_ticks


def record_path(name: str) -> Path:
    """Where the named session's on-disk record lives —
    ``$AVA_HOME/run/sessions/<name>.json``.

    Public so a caller can PRE-write a record before the session process
    exists (the update chain lands its session record before it lands its
    pause/checkout work — P2 #2102); the real record written at spawn replaces
    it atomically."""
    import base.paths

    return base.paths.run_dir() / "sessions" / f"{name}.json"


@dataclass(frozen=True)
class SessionRecord:
    """A launched background session's identity + provenance.

    `pid` + `starttime` are the liveness key on Linux; `create_time` is the
    compatibility fallback for legacy records. A matching process
    start-time defeats pid recycling. `cmd` / `cwd` / `started_at` are diagnostic
    provenance. `generation` is the admitting allocation/runtime generation:
    PTYs use their allocation, admitted agent records their runtime incarnation.
    It is a derived observation, not permission to claim work or signal a PID.
    Legacy and other service records leave it null.
    """

    pid: int
    create_time: float
    cmd: str
    cwd: str
    started_at: float
    starttime: int | None = None
    generation: str | None = None
    control_mode: str | None = None
    # The session's process group at spawn (the reparent helper's
    # setsid pgid). The durable ownership proof for a leader that already died:
    # while the group is occupied, its surviving members are still this
    # session's descendants and a stop may converge them instead of silently
    # losing them. None on legacy records.
    pgid: int | None = None

    @classmethod
    def read(cls, path: Path) -> SessionRecord | None:
        """Parse the record at `path`, or None if it is absent / unreadable /
        not a well-formed record (a teardown race or a truncated write)."""
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            return None
        if not isinstance(data, dict) or "pid" not in data:
            return None
        record = cast("dict[str, Any]", data)
        return cls(
            pid=int(record["pid"]),
            create_time=float(record.get("create_time", 0.0)),
            cmd=str(record.get("cmd", "")),
            cwd=str(record.get("cwd", "")),
            started_at=float(record.get("started_at", 0.0)),
            starttime=(None if record.get("starttime") is None else int(record["starttime"])),
            generation=(
                record["generation"]
                if isinstance(record.get("generation"), str) and record["generation"]
                else None
            ),
            control_mode=record.get("control_mode"),
            pgid=int(record["pgid"]) if isinstance(record.get("pgid"), int) else None,
        )

    def identifies(self, pid: int) -> bool | None:
        """Whether `pid` is this record's process by its stable Linux identity.

        False proves the pid is another process; None means a legacy
        record or an unavailable `/proc` reading, whose callers fall back to
        `create_time` where that compatibility behavior is required.
        """
        if pid != self.pid:
            return False
        if self.starttime is None:
            return None
        actual_starttime = pid_starttime_ticks(pid)
        if actual_starttime is None:
            return None
        return actual_starttime == self.starttime

    def write(self, path: Path) -> None:
        """Persist as JSON at `path` (the shape `read` parses back), atomically.

        Temp file + rename, so a concurrent reader never sees a truncated
        write: `read` treats an unparseable record as absent, and the session
        listings (`list_sessions`) then unlink a record
        whose process is still alive — a live agent silently forgotten by
        `ava stop`'s no-DB reap (audit 2026-08-08 P1). Same shape
        `base/deploy/lifecycle/launch_failures.py` uses for the same reason."""
        path.parent.mkdir(parents=True, exist_ok=True)
        _fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(_fd, "w") as f:
                json.dump(asdict(self), f)
            Path(tmp).replace(path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp).unlink(missing_ok=True)
            raise
