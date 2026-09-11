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

# create_time is not bit-stable for one live process: psutil's macOS
# implementation re-derives it from the wall clock and applies a boot-time
# correction quantized to whole seconds, and WSL wall-clock steps move it too.
# Measured 2026-09-12 (company-mini): the stop path refused a live host over
# exactly 1.000000s of drift. Pid reuse cannot land inside a couple of seconds,
# so the create_time fallback compares with the same 2.0s tolerance the other
# create_time identity checks use (posixproc / winproc / proc).
_CREATE_TIME_TOLERANCE_S = 2.0


def create_time_matches(live: float, birth: float) -> bool:
    """Whether a live create_time reading still claims the recorded birth.

    Every comparison of a re-read create_time must carry the tolerance a
    reading moves by for one live process (see `_CREATE_TIME_TOLERANCE_S`);
    comparing persisted values that were never re-derived stays exact.
    """
    return abs(live - birth) <= _CREATE_TIME_TOLERANCE_S


@dataclass(frozen=True)
class OwnedProcess:
    pid: int
    birth: float
    starttime: int | None

    @classmethod
    def capture(cls, process: psutil.Process) -> OwnedProcess:
        return cls(process.pid, process.create_time(), pid_starttime_ticks(process.pid))

    def birth_matches(self, process: psutil.Process) -> bool:
        """Whether `process`'s create_time still claims this identity's birth.

        The exact identity is the Linux `starttime` tick, which `live()` checks
        first; this is the fallback used where the platform has none, and it
        must tolerate the whole-second moves a create_time reading makes while
        its process stays alive (see `_CREATE_TIME_TOLERANCE_S`).
        """
        return create_time_matches(process.create_time(), self.birth)

    def live(self) -> bool:
        try:
            process = psutil.Process(self.pid)
            if self.starttime is not None:
                actual = pid_starttime_ticks(self.pid)
                if actual is None:
                    raise RuntimeError(f"cannot verify process identity for PID {self.pid}")
                if actual != self.starttime:
                    return False
            elif not self.birth_matches(process):
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
