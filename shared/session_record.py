"""On-disk session record shared by the POSIX / Windows process supervisors.

`shared.posixproc` and `shared.winproc` are one-to-one platform mirrors that
persist a launched background session as JSON under
`$AVA_HOME/run/sessions/<name>.json`. This is the single shape both sides use —
`new_session` writes it, `_process_for_record` reads it back — so the field
contract lives in one typed place instead of two mirrored literal dicts.

On Linux, WSL2 may step `/proc/stat`'s `btime`, which makes psutil's epoch
`create_time()` drift for a still-live pid. `starttime` records `/proc/<pid>/stat`
field 22 instead: clock ticks since boot are monotonic and therefore the stable
process identity when available; `create_time` remains for Windows and legacy
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


def pid_starttime_ticks(pid: int) -> int | None:
    """Linux `/proc` start time in clock ticks since boot, or None when unavailable.

    The command name in field 2 may include spaces or parentheses, so split the
    stat record only after its final closing parenthesis.
    """
    try:
        line = Path(f"/proc/{pid}/stat").read_text()
        rest = line.rsplit(")", 1)[1].split()
        return int(rest[22 - 3])
    except (IndexError, OSError, ValueError):
        return None


@dataclass(frozen=True)
class SessionRecord:
    """A launched background session's identity + provenance.

    `pid` + `starttime` are the liveness key on Linux; `create_time` is the
    compatibility fallback for legacy and Windows records. A matching process
    start-time defeats pid recycling. `cmd` / `cwd` / `started_at` are diagnostic
    provenance. `generation` is the admitting allocation/runtime generation:
    PTYs use their allocation, admitted agent records their runtime incarnation.
    It is a derived observation, not permission to claim work or signal a PID.
    Legacy and other service records leave it null.
    On Windows a Python venv redirector can be the native session/group control
    PID while agents_meta.pid remains the admitted interpreter child. Publication
    must verify that direct wrapper ancestry and birth identity, never guess it.
    """

    pid: int
    create_time: float
    cmd: str
    cwd: str
    started_at: float
    starttime: int | None = None
    generation: str | None = None
    control_mode: str | None = None

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
        )

    def identifies(self, pid: int) -> bool | None:
        """Whether `pid` is this record's process by its stable Linux identity.

        False proves the pid is another process; None means a legacy/Windows
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
        listings (`list_sessions` on both platforms) then unlink a record
        whose process is still alive — a live agent silently forgotten by
        `ava stop`'s no-DB reap (audit 2026-08-08 P1). Same shape
        `shared/launch_failures.py` uses for the same reason."""
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
