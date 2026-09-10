"""PID/birth-validated process-tree ownership shared by the stop path and probes.

The stop boundary (``cli.commands._maintenance_stop``) and the frontend identity
probe (``services.healthchecks.frontend``) must answer the same question — "is
this pid the recorded session's leader or one of its descendants, still the
process it was?" — so the primitive lives here, importable by both without the
ops layer reaching through services into cli (issue #2123).
"""

from __future__ import annotations

from dataclasses import dataclass

import psutil

from shared.session_record import SessionRecord, pid_starttime_ticks


@dataclass(frozen=True)
class OwnedProcess:
    pid: int
    birth: float
    starttime: int | None

    @classmethod
    def capture(cls, process: psutil.Process) -> OwnedProcess:
        return cls(process.pid, process.create_time(), pid_starttime_ticks(process.pid))

    def live(self) -> bool:
        try:
            process = psutil.Process(self.pid)
            if self.starttime is not None:
                actual = pid_starttime_ticks(self.pid)
                if actual is None:
                    raise RuntimeError(f"cannot verify process identity for PID {self.pid}")
                if actual != self.starttime:
                    return False
            elif process.create_time() != self.birth:
                return False
            return process.status() not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD)
        except psutil.NoSuchProcess:
            return False


def capture_tree(identity: OwnedProcess) -> set[OwnedProcess]:
    """Capture descendants while the parent's birth identity still matches."""
    if not identity.live():
        return set()
    try:
        children = psutil.Process(identity.pid).children(recursive=True)
        captured = {identity}
        for child in children:
            try:
                captured.add(OwnedProcess.capture(child))
            except psutil.NoSuchProcess:
                continue
        # A PID replacement during enumeration invalidates the capture. Never
        # attach a replacement process's descendants to the original identity.
        if not identity.live():
            try:
                current = OwnedProcess.capture(psutil.Process(identity.pid))
            except psutil.NoSuchProcess:
                return captured
            if current.live():
                raise RuntimeError(f"process changed during descendant capture: {identity.pid}")
        return captured
    except psutil.NoSuchProcess:
        return {identity}


def session_owns_pids(record: SessionRecord, pids: set[int]) -> bool:
    """Whether any pid in `pids` is `record`'s leader or a birth-validated descendant.

    False when the leader is gone: a descendant whose leader died carries no
    proof of whose it is (the stop path converges such survivors through the
    recorded process group, a stronger claim than this probe).
    """
    leader = OwnedProcess(record.pid, record.create_time, record.starttime)
    if not leader.live():
        return False
    owned = {identity.pid for identity in capture_tree(leader)}
    return bool(owned & pids)
