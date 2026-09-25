"""PID/birth-validated process-tree ownership shared by the stop path and probes.

The stop boundary (``cli.commands._maintenance_stop``) and the frontend identity
probe (``services.healthchecks.frontend``) must answer the same question — "is
this pid the recorded session's leader or one of its descendants, still the
process it was?" — so the primitive lives here, importable by both without the
ops layer reaching through services into cli (issue #2123).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any, cast

import psutil

from shared.session_record import pid_starttime_ticks

# Identity reads use the stable start timestamp (stable_create_time): on macOS
# the uncorrected kernel value, elsewhere the public create_time(). The public
# macOS value re-derives from the wall clock with a boot-time correction
# quantized to whole seconds (measured 2026-09-12: one live process read
# 1.000000s apart by two import epochs), and WSL wall-clock steps move the
# Linux value too. Linux uses its starttime tick; other native birth readings
# compare exactly. Legacy records are not silently adopted across this boundary.


def create_time_matches(live: float, birth: float) -> bool:
    """Whether two stable native birth readings identify exactly the same process."""
    return live == birth


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
        stable start time (`stable_create_time`) exactly. An old wall-clock
        reading cannot authorize custody over a newly captured native process.
        """
        return create_time_matches(stable_create_time(process), self.birth)

    def live(self) -> bool:
        """Whether the pid still is this recorded process.

        A tracked process exiting and being reaped exactly between psutil's
        eager validation and the raw start-time read leaves that read without a
        /proc entry — that IS the exit, not an unverifiable identity: existence
        is re-asked, and a vanished pid converges as gone (the 2026-09-20
        wave-2 window raised out of a stop this way and aborted the cluster
        update with the tracked daemon already exiting). Only a pid that still
        exists while its start time cannot be read keeps the loud error.
        """
        try:
            process = psutil.Process(self.pid)
            if self.starttime is not None:
                actual = pid_starttime_ticks(self.pid)
                if actual is None:
                    if not psutil.pid_exists(self.pid):
                        # Reaped in the validation -> read window: exited.
                        return False
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


def leader_owns_pids(leader: OwnedProcess, pids: set[int]) -> bool:
    """Whether any pid in `pids` is `leader` or a birth-validated descendant.

    False when the leader is gone: a descendant whose leader died carries no
    proof of whose it is (the stop path converges such survivors through the
    recorded process group, a stronger claim than this probe). The identity
    question is the same whether the leader came from a session record or, on
    a root-driven host, from a tree unit's row (task #3370) — only the source
    of `leader` differs, never this rule.
    """
    if not leader.live():
        return False
    owned = {identity.pid for identity in capture_tree(leader)}
    return bool(owned & pids)


def process_metadata() -> dict[str, Any]:
    """Observed process facts for one caller, standardized for attestation.

    Identity fields use the stable start time (`stable_create_time`) so a
    later reader compares against the same value the kernel keeps. The parent
    walk is bounded and records what it could observe instead of failing: a
    permission gap on an ancestor path must not lose the facts below it.
    """
    result: dict[str, Any] = {"pid": os.getpid(), "ancestors": []}
    # env-ok: inherited provider routing context, never an executor identity assertion
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        result["codex_home"] = codex_home
    process = psutil.Process()
    for depth in range(8):
        try:
            facts = {
                "pid": process.pid,
                "name": process.name(),
                "executable": process.exe(),
                "created_at": stable_create_time(process),
                "parent_pid": process.ppid(),
            }
            if depth == 0:
                result.update(facts)
            else:
                result["ancestors"].append(facts)
            parent = process.parent()
            if parent is None:
                break
            process = parent
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            result["observation_error"] = type(exc).__name__
            break
    return result
