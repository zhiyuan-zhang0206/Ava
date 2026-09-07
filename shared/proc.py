"""Local OS process primitives: the liveness probe, the force-kill, and the
timeout that actually bounds the work (`run_bounded`).

`process_alive` and `process_cmdline` inspect local processes. A PID from
another machine is not ours to probe, and PID liveness alone is not identity:
callers that act on an execution owner must also verify its OS birth evidence.

`run_bounded` is the module's other half and exists because
`subprocess.run(..., timeout=T)` does not do what its name implies: on expiry
Python kills the **one** process it spawned and leaves every descendant running.
Measured consequence on the fleet's Windows agent-runner: 66 orphaned `git.exe`
+ 66 `ssh.exe` + 63 `sh.exe`, because `C:\\Program Files\\Git\\cmd\\git.exe` is a
thin launcher for the real git — the timeout killed the launcher and the
three-process tail below it survived. Every Ava-initiated subprocess with a
timeout should go through `run_bounded` instead, so the bound applies to the
work rather than to whichever wrapper happened to be on top.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Sequence
from typing import Any

import psutil

from shared.paths import run_dir
from shared.platform import CREATE_NO_WINDOW, SIGKILL
from shared.platform_backend import get_backend

# psutil exceptions that mean "the process is already gone / not ours to touch" —
# expected during a teardown, not an error: between enumerating a tree and
# signalling it, any member may exit on its own.
_GONE = (psutil.NoSuchProcess, psutil.AccessDenied, OSError)

# How long the tree gets between the terminate and the kill. This is NOT
# politeness — the caller's `timeout` already was all the patience the work
# gets. It buys exactly one thing: git installs signal handlers that unlink its
# lockfiles (`.git/index.lock`), so a SIGTERM'd `git pull` leaves a usable repo
# where a SIGKILL'd one can leave a lock that breaks the next git call. Seconds,
# not tens of seconds, because the thing we are killing has already proven it
# does not finish. On Windows psutil maps both terminate() and kill() to
# TerminateProcess, so the ladder collapses to a single hard kill there at no
# cost — which is also why this is not branched on IS_WINDOWS.
_TERMINATE_GRACE_S = 3.0

# Ceiling on the post-kill reap. A process that survives SIGKILL is in
# uninterruptible sleep (wedged NFS / a stuck driver) and no amount of waiting
# will collect it; we log nothing and return rather than hang the caller.
_REAP_TIMEOUT_S = 5.0

# Ceiling on draining the pipes after the tree is dead. The descendants
# inherited the write ends, so an un-killable holder would make an unbounded
# drain the new hang — the whole defect one layer down.
_DRAIN_TIMEOUT_S = 5.0

# The detached orchestration sessions — `ops/cluster_session.py`'s service names
# through `shared.cluster.session_name` — are the sanctioned hosts of an
# in-process host transition (the stop leg does not target them), so
# `hosting_supervised_session` exempts them. Spelled as the composed literals to
# keep this leaf module below `shared.cluster` in the import graph. The dry-run
# exemption lets its detached child reach the non-mutating `--local --dry-run`
# leg; it remains outside the deploy in-flight scan in `ops.cluster_session`.
_ORCHESTRATION_SESSIONS = frozenset(
    {"ava-rollout", "ava-rollout-dryrun", "ava-updater", "ava-cluster-restart"}
)

# A recorded pid counts as the recorded session only while the live process's
# start time matches the record — the supervisors' own pid-recycling rule
# (`posixproc` / `winproc` / the pty backend all use this same 2 s tolerance).
_SESSION_CREATE_TIME_TOLERANCE_S = 2.0


def hosting_supervised_session() -> str | None:
    """The supervised session this process is running INSIDE — the name of a live
    `$AVA_HOME/run/sessions/<name>.json` record whose pid is this process or one
    of its ancestors — or None when the lineage is clear.

    The question a host-transition verb must ask before running in-process: the
    stop leg of an update/restart kills every service session's whole tree, and
    agents + their shells are quiesced/reaped with it, so an orchestration
    launched from inside one of those trees is killed by its own stop mid-flight
    (2026-08-12: an agent ran `ava cluster update --local` in its pty-hosted
    background shell; stopping ava-pty-supervisor force-killed the supervisor's
    whole tree — rollout included — stranding the cluster paused with every
    service down). Windows preserves nested live-session subtrees during a kill,
    so its 2026-08-24 updater restart is safe inside `ava-ops`; the guard asks the
    kill path's predicate rather than approximating that boundary. The detached
    orchestration sessions are exempt: they are exactly the hosts the detached
    form sanctions.

    A record whose process is gone, or whose pid the OS recycled onto a
    different process (start-time mismatch), does not count.
    """
    # Deliberately method-local: the in-process updater's post-checkout stop must
    # load these from the new tree; a module-scope import leaves their old version
    # in sys.modules before checkout. See shared/session_backend.py.
    from shared import winproc
    from shared.session_record import SessionRecord

    try:
        me = psutil.Process()
        lineage = {me.pid} | {p.pid for p in me.parents()}
    except psutil.Error:  # racing our own ancestry going away — no lineage evidence
        return None
    for record_path in (run_dir() / "sessions").glob("*.json"):
        name = record_path.stem
        if name in _ORCHESTRATION_SESSIONS:
            continue
        record = SessionRecord.read(record_path)
        if record is None or record.pid not in lineage:
            continue
        try:
            proc = psutil.Process(record.pid)
            if record.starttime is not None:
                is_record_process = record.identifies(record.pid) is True
            else:
                is_record_process = (
                    abs(proc.create_time() - record.create_time) <= _SESSION_CREATE_TIME_TOLERANCE_S
                )
            if is_record_process and not winproc.tree_kill_would_spare(name, proc, lineage):
                return name
        except psutil.Error:
            continue
    return None


def process_alive(pid: int) -> bool:
    """Liveness probe.

    True if `pid` names a live process; False only on an unambiguous
    "no such pid". A PermissionError — the pid was recycled and is now owned
    by another user — counts as alive: we must not declare a row an orphan
    when its pid maps to *some* running process.

    POSIX uses `os.kill(pid, 0)` (signal 0 — existence test, no signal sent).
    On Windows `os.kill(pid, 0)` would call TerminateProcess and *kill* the
    target, so we probe via psutil instead (a pure handle query).
    """
    backend = get_backend()
    if not backend.is_posix():  # Windows: must avoid os.kill(pid, 0)
        return backend.process_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def process_cmdline(pid: int) -> list[str] | None:
    """The OS-level argv `pid` was exec'd with, or None if it cannot be read.

    None means "no answer", never "no arguments". A pid that is gone, one whose
    argv this user may not read (a recycled pid now owned by someone else), and a
    zombie (whose argv the kernel has already released, surfaced as an empty
    list) all collapse to None, because none of them is evidence about *whose*
    process the pid is. A caller that must tell "gone" from "alive but opaque"
    pairs this with `process_alive` — the pair is what `ops.agent_identity` turns
    into a verdict.

    Unlike `os.kill`, there is no Windows hazard here: psutil reads the process
    table directly (`/proc/<pid>/cmdline` on Linux, `KERN_PROCARGS2` on macOS, the
    PEB on Windows) and delivers nothing to the target, so this is not branched on
    the backend.
    """
    try:
        cmdline = psutil.Process(pid).cmdline()
    except (psutil.Error, OSError):
        return None
    return cmdline or None


def force_kill(pid: int) -> None:
    """Force-terminate `pid` (the SIGKILL intent), tolerant of an absent pid.

    POSIX sends SIGKILL. On Windows `os.kill(pid, SIGKILL-alias)` would map to
    TerminateProcess but SIGKILL is undefined, so terminate via psutil
    (TerminateProcess under the hood). A dead/absent pid is a silent no-op —
    callers force-kill exactly to make a row reach 'terminated', so racing the
    process's own exit must not raise.

    A pid this user may not signal is a no-op too, and for the same reason as
    `process_alive` reading it as alive: "could not deliver" is not "did not need
    to". Raising here would put an unhandled exception in the middle of a stop —
    the shape `_terminate_verified` was rewritten to remove — where the honest
    outcome is that the caller re-probes and reports the process a survivor.
    """
    backend = get_backend()
    if not backend.is_posix():  # Windows: must avoid os.kill(SIGKILL)
        backend.force_kill(pid)
        return

    try:
        os.kill(pid, SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def request_stop(pid: int) -> None:
    """Ask `pid` to stop (the SIGTERM intent), tolerant of an absent pid.

    The missing third of the pair `process_alive` / `force_kill` already form, and
    it is missing for the reason those two exist: a caller that wants the polite
    first pass has to reach for a signal, and the obvious spelling —
    `os.kill(pid, SIGTERM)` — is one of the two that do not survive the crossing to
    Windows. Every escalating stop in this repo needs all three, so all three
    belong here rather than being re-derived, correctly or otherwise, per caller.

    **On Windows this is not gentler than `force_kill`, and that is stated rather
    than hidden.** There is no signal to deliver to an arbitrary process: Ctrl-Break
    reaches only a process group we own (`shared.winproc.graceful_signal`), and
    TerminateProcess — what psutil's `terminate()` calls — is uncatchable. A caller
    escalating request_stop -> wait -> force_kill therefore gets a real grace period
    on POSIX and an immediate stop on Windows. That is a platform fact, not a bug to
    paper over: the alternative is a call that raises where it used to work.

    A pid this user may not signal returns quietly, like `force_kill` and for the
    same reason (see there). The realistic shape is not exotic: a stray from a
    `sudo`-run instance of this very checkout passes the caller's cmdline ownership
    check, `process_alive` correctly reads it as alive, and the signal is refused.
    """
    backend = get_backend()
    if not backend.is_posix():  # Windows: no SIGTERM to deliver, and os.kill lies
        backend.force_kill(pid)
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return


def kill_process_tree(pid: int, *, grace_s: float = _TERMINATE_GRACE_S) -> None:
    """Take down `pid` **and every descendant**: terminate the tree, wait up to
    `grace_s`, then hard-kill whatever is still standing. A pid that is already
    gone is a no-op.

    Escalation (terminate → wait → kill) rather than a straight kill for one
    reason only, spelled out at `_TERMINATE_GRACE_S`: git unlinks its lockfiles
    from a signal handler. Nothing here waits on the tree's cooperation — the
    kill is unconditional after `grace_s`.

    The descendant set is enumerated **once, up front, while the parent is still
    alive**. Walking down from a dead parent is not possible: psutil resolves
    children by ppid, and on Windows there is no reparent-to-init to walk to
    instead — the link is simply lost, which is how a tree survives a kill that
    was aimed at its root. Order is descendants-then-parent so a supervising
    parent cannot respawn a child mid-teardown.

    Limitation worth knowing: a descendant that has already double-forked away
    (reparented to init) is not in the ppid walk and is not reached. git and ssh
    do not do that, so the measured leak shape is covered; a process supervisor
    is not something to bound with a timeout in the first place.
    """
    try:
        parent = psutil.Process(pid)
        tree = [*parent.children(recursive=True), parent]
    except _GONE:
        return

    for proc in tree:
        # A member that exited between enumeration and this line is the normal
        # case, not a failure — that race is the whole reason the set is
        # snapshotted rather than re-walked.
        with contextlib.suppress(*_GONE):
            proc.terminate()
    _gone, alive = psutil.wait_procs(tree, timeout=grace_s)
    if not alive:
        return
    for proc in alive:
        with contextlib.suppress(*_GONE):
            proc.kill()
    psutil.wait_procs(alive, timeout=_REAP_TIMEOUT_S)


def run_bounded(
    argv: Sequence[str],
    *,
    timeout: float,
    capture_output: bool = False,
    **popen_kwargs: object,
) -> subprocess.CompletedProcess[Any]:  # str or bytes, decided by the caller's `text=`
    """`subprocess.run` whose timeout bounds the **work**, not just the process
    Python spawned: on expiry the whole tree dies (`kill_process_tree`) before
    `TimeoutExpired` propagates.

    Drop-in for the `subprocess.run(argv, timeout=..., check=False)` shape:
    `capture_output` is honoured, everything else is passed through to `Popen`
    (`cwd`, `text`, `env`, …). There is deliberately no `check=` — this returns
    the `CompletedProcess` and the caller reads `returncode`, so a non-zero exit
    can never be confused with the timeout path.

    `TimeoutExpired` is raised exactly as `subprocess.run` raises it, carrying
    whatever output was captured before the bound tripped: a caller that treats a
    timeout as a failed fetch keeps working unchanged. This fixes the leak, not
    the control flow.

    Raises:
        subprocess.TimeoutExpired: the tree did not finish within `timeout`. It
            is dead by the time this is raised.
    """
    if capture_output:
        if "stdout" in popen_kwargs or "stderr" in popen_kwargs:
            raise ValueError("capture_output=True is exclusive with stdout / stderr")
        popen_kwargs["stdout"] = subprocess.PIPE
        popen_kwargs["stderr"] = subprocess.PIPE

    if "creationflags" not in popen_kwargs:
        popen_kwargs["creationflags"] = CREATE_NO_WINDOW

    proc = subprocess.Popen(argv, **popen_kwargs)  # type: ignore[call-overload]  # noqa: S603 — argv is list-form, callers pass fixed argv
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # `kill_process_tree` may reap this child itself (psutil waitpid's its
        # own children), leaving `proc.returncode` meaningless afterwards — fine
        # on this path, which raises rather than returning a CompletedProcess.
        kill_process_tree(proc.pid)
        # The pipes' write ends were inherited by the descendants, so this drain
        # only terminates because they are dead — bounded anyway, because an
        # un-killable holder would otherwise make the drain the new unbounded
        # wait (the same defect, one layer down). `exc` already carries whatever
        # was buffered before the bound tripped, so a drain that times out keeps
        # that partial output.
        with contextlib.suppress(subprocess.TimeoutExpired):
            exc.stdout, exc.stderr = proc.communicate(timeout=_DRAIN_TIMEOUT_S)
        raise
    except BaseException:  # KeyboardInterrupt / cancellation must not leak a tree either
        kill_process_tree(proc.pid)
        proc.poll()  # collect the status if psutil did not, so Popen leaves no zombie
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def timeout_stderr_tail(exc: subprocess.TimeoutExpired, *, lines: int = 3) -> str:
    """The last `lines` non-empty stderr lines a timed-out child wrote, as text.

    A `run_bounded` timeout's `TimeoutExpired` carries whatever the child wrote
    before the bound tripped (the pipes are drained after the tree kill) — the
    "where was it when it died" evidence a bare timeout message drops. Bytes or
    None are normalized so callers can log/return the tail without type games:
    a fetch killed before ssh/git printed anything yields an empty string, which
    is itself the evidence that it died in the local/connect phase rather than
    mid-transfer (2026-08-27 win/wsl fetch forensics).
    """
    raw: str | bytes | None = exc.stderr
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return " | ".join((raw or "").strip().splitlines()[-lines:])
