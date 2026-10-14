"""`ProcessControl`: the port for signalling processes and reading their groups and sessions.

Signalling a process or a process group, and asking which group or session a pid belongs to,
are the calls whose failure a test has to cause: a denied signal, a group that stays live after
SIGKILL, a process that vanished between two reads. A component that does any of these takes
`process_control: ProcessControl` as a required argument (no default) and calls it instead of
`os.kill`, `os.killpg`, `os.getpgid` and `os.getsid`. The composition root passes
`SYSTEM_PROCESS_CONTROL`; a test passes `FakeProcessControl` from
`base/native_process/tests/fake_process_control.py`, or the system one when it signals a real
child it started itself. A test never patches `os.kill` and its siblings.

Observing who is in a group (psutil, `/proc`, libproc) is not signalling and stays in
`group_closure` and `ownership`; the port holds only the four calls above.
"""

from __future__ import annotations

import os
from typing import Protocol


class ProcessControl(Protocol):
    """The four process calls a component makes through the operating system.

    Every parameter is positional so that a fake may name them freely. Each method raises what
    the matching `os` function raises (`ProcessLookupError`, `PermissionError`).
    """

    def kill(self, pid: int, signum: int, /) -> None:
        """Send `signum` to one process; signal 0 only probes that it exists."""
        ...

    def killpg(self, pgid: int, signum: int, /) -> None:
        """Send `signum` to every member of a process group; signal 0 only probes the group."""
        ...

    def getpgid(self, pid: int, /) -> int:
        """The process group id of `pid`."""
        ...

    def getsid(self, pid: int, /) -> int:
        """The session id of `pid`."""
        ...


class _SystemProcessControl:
    """The real calls."""

    def kill(self, pid: int, signum: int, /) -> None:
        os.kill(pid, signum)

    def killpg(self, pgid: int, signum: int, /) -> None:
        os.killpg(pgid, signum)

    def getpgid(self, pid: int, /) -> int:
        return os.getpgid(pid)

    def getsid(self, pid: int, /) -> int:
        return os.getsid(pid)


SYSTEM_PROCESS_CONTROL: ProcessControl = _SystemProcessControl()
