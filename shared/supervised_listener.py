"""Identity-verified takeover of a supervised native-service listener.

A native service detaches from its launcher, so PPID is no ownership proof:
the session record names the supervised PID. ``probe_supervised_listener``
classifies every holder of a service's ports as supervised, reclaimable stale
(same binary, no live record — a collector that survived its supervisor), or
foreign (different executable — never touched). ``reclaim_stale_supervised_
listener`` evicts only the verified stale holders before a relaunch, so a
replacement never dies on a port an impostor holds.

The kill chain (``shared.session_backend``) and its ``shared.session_record``
dependency are imported method-locally, matching the update path's
import-closure invariant (tests/cli/test_update_import_timing.py, PR #932):
nothing the updater imports before its git checkout may load the session kill
chain, or the in-process stop after checkout runs pre-pull kill code.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from shared.cluster import session_name
from shared.daemon_health import DaemonProbe
from shared.paths import run_dir
from shared.port_preflight import listeners_on
from shared.proc import force_kill, process_alive, request_stop

_log = logging.getLogger("shared.supervised_listener")

# A stale native listener is not a reason to stall a 60 s watchdog round. Five
# seconds leaves a cooperative collector enough time to run its shutdown path;
# the verified SIGKILL fallback is unconditional once that bounded window ends.
_STALE_LISTENER_TIMEOUT_S = 5.0
_TERMINATE_POLL_S = 0.1
_FORCE_KILL_SETTLE_S = 0.5

StaleListenerStop = Literal["noop", "graceful", "forced", "survivor"]


@dataclass(frozen=True)
class SupervisedListenerProbe:
    probe: DaemonProbe
    stale_pids: tuple[int, ...] = ()


def _listener_matches_binary(pid: int, binary: Path) -> bool:
    import psutil

    try:
        return Path(psutil.Process(pid).exe()).resolve() == binary.resolve()
    except (psutil.Error, OSError):
        return False


def probe_supervised_listener(
    service: str, *, ports: tuple[int, ...], binary: Path
) -> SupervisedListenerProbe:
    """Classify listeners as supervised, reclaimable stale, or foreign.

    Native services detach, so PPID=1 is insufficient; the record names the PID.
    """
    session = session_name(service)
    # Method-local: the update path's import closure must not load the kill
    # chain before its git checkout (see the module docstring).
    from shared.session_backend import get_backend
    from shared.session_record import SessionRecord

    record = SessionRecord.read(run_dir() / "sessions" / f"{session}.json")
    supervised = record is not None and get_backend().has_session(session)
    holders = {port: tuple(dict.fromkeys(listeners_on(port))) for port in ports}
    pids = tuple(dict.fromkeys(pid for port in ports for pid in holders[port]))
    expected = tuple(pid for pid in pids if _listener_matches_binary(pid, binary))
    stale = tuple(pid for pid in expected if not supervised or record is None or record.pid != pid)
    foreign = tuple(pid for pid in pids if pid not in expected)
    if foreign:
        return SupervisedListenerProbe(
            DaemonProbe.port_taken(
                f"{service} ports {ports} include pid(s) {foreign} not executing {binary}"
            ),
            stale,
        )
    if not holders[ports[0]]:
        return SupervisedListenerProbe(
            DaemonProbe.down(f"nothing listening on {service} port {ports[0]}"), stale
        )
    if stale:
        return SupervisedListenerProbe(
            DaemonProbe.down(
                f"{service} listener pid(s) {stale} execute {binary} without a live {session} record"
            ),
            stale,
        )
    pid = record.pid if record is not None else "unknown"
    return SupervisedListenerProbe(DaemonProbe.up(f"{service} listener pid {pid} is supervised"))


def _wait_for_listener_exit(pid: int, timeout_s: float) -> bool:
    """Wait only until `timeout_s` expires; `True` means the PID is gone."""
    deadline = time.monotonic() + timeout_s
    while process_alive(pid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_TERMINATE_POLL_S, remaining))
    return True


def terminate_stale_listener(
    pid: int, *, grace_s: float = _STALE_LISTENER_TIMEOUT_S
) -> StaleListenerStop:
    """Stop a verified stale listener and report its verified final state.

    An absent PID is a no-op. A live one always receives SIGTERM first, then
    receives SIGKILL after the bounded grace window unless it already exited.
    The short post-kill wait prevents a signal-delivery request from being
    mistaken for a completed stop; an unkillable process is a `survivor` for
    the caller to report rather than a false recovery.
    """
    if grace_s < 0:
        raise ValueError("grace_s must not be negative")
    if not process_alive(pid):
        return "noop"
    request_stop(pid)
    if _wait_for_listener_exit(pid, grace_s):
        return "graceful"
    force_kill(pid)
    if _wait_for_listener_exit(pid, _FORCE_KILL_SETTLE_S):
        return "forced"
    return "survivor"


def reclaim_stale_supervised_listener(
    service: str,
    *,
    ports: tuple[int, ...],
    binary: Path,
    grace_s: float = _STALE_LISTENER_TIMEOUT_S,
) -> SupervisedListenerProbe:
    """Evict only verified stale holders, then report what remains.

    Each stale PID gets one bounded SIGTERM window before the verified SIGKILL
    fallback. A survivor remains visible in the returned probe and is logged
    explicitly, so its port cannot be mistaken for successfully reclaimed.
    """
    result = probe_supervised_listener(service, ports=ports, binary=binary)
    for pid in result.stale_pids:
        if pid in probe_supervised_listener(service, ports=ports, binary=binary).stale_pids:
            outcome = terminate_stale_listener(pid, grace_s=grace_s)
            if outcome == "survivor":
                _log.error(
                    "%s stale listener pid %s survived forced stop; leaving it visible for recovery",
                    service,
                    pid,
                )
    return probe_supervised_listener(service, ports=ports, binary=binary)
