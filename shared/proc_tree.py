"""PID/birth-validated process-tree ownership shared by the stop path and probes.

The stop boundary (``cli.commands._maintenance_stop``) and the frontend identity
probe (``services.healthchecks.frontend``) must answer the same question — "is
this pid the recorded session's leader or one of its descendants, still the
process it was?" — so the primitive lives here, importable by both without the
ops layer reaching through services into cli (issue #2123).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, cast

import psutil

from shared.session_record import SessionRecord, pid_starttime_ticks

# Identity reads use the stable start timestamp (stable_create_time): on macOS
# the uncorrected kernel value, elsewhere the public create_time(). The public
# macOS value re-derives from the wall clock with a boot-time correction
# quantized to whole seconds (measured 2026-09-12: one live process read
# 1.000000s apart by two import epochs), and WSL wall-clock steps move the
# Linux value too. Records written before the stable key existed — and any
# platform without one — still compare with a 2.0s tolerance; new records read
# back exactly. Pid reuse cannot land inside a couple of seconds.
_CREATE_TIME_TOLERANCE_S = 2.0


def create_time_matches(live: float, birth: float) -> bool:
    """Whether a live create_time reading still claims the recorded birth.

    Every comparison of a re-read create_time must carry the tolerance a
    reading moves by for one live process (see `_CREATE_TIME_TOLERANCE_S`);
    comparing persisted values that were never re-derived stays exact.
    """
    return abs(live - birth) <= _CREATE_TIME_TOLERANCE_S


def stable_create_time(process: psutil.Process) -> float:
    """The stable start timestamp of a live process: the identity key.

    psutil's public `create_time()` is not bit-stable across readers on macOS:
    it adds a whole-second wall-clock correction on top of the kernel value
    (and, since psutil 7.2, caches it per `Process` instance), so two readings
    of one live process taken by different import epochs disagree by exactly
    the correction. The uncorrected kernel value is what psutil itself keys
    pid reuse on — `Process._proc.create_time(monotonic=True)`, documented
    "stable over changes to system time" — and identity records must be
    written and compared through that same value. Elsewhere the public value
    already is the kernel start timestamp.
    """
    if sys.platform == "darwin":
        # psutil's per-platform object is untyped in the public stubs; its
        # monotonic create_time is the pid-reuse key used above.
        return float(cast("Any", process)._proc.create_time(monotonic=True))
    return process.create_time()


@dataclass(frozen=True)
class OwnedProcess:
    pid: int
    birth: float
    starttime: int | None

    @classmethod
    def capture(cls, process: psutil.Process) -> OwnedProcess:
        return cls(process.pid, stable_create_time(process), pid_starttime_ticks(process.pid))

    def birth_matches(self, process: psutil.Process) -> bool:
        """Whether `process`'s stable start time still claims this birth.

        The exact identity is the Linux `starttime` tick, which `live()` checks
        first; this is the fallback where the platform has none, reading the
        stable start time (`stable_create_time`) and keeping the 2.0s
        tolerance for records written before it existed.
        """
        return create_time_matches(stable_create_time(process), self.birth)

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
