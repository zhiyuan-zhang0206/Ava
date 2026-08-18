"""Reach the Chrome that left the supervised process tree.

`ava stop --stop-browser` and `ava cluster destroy` are the two paths that mean
"this cluster's headed Chrome goes down too", and they say it by killing the
`ava-browser` session's process tree. That is enough while Chrome is *in* the
tree. It stops being enough after a `SingletonLock` handoff: Chrome's launched
process forwards its command line to another browser process and exits, and the
survivor is no longer a descendant of the session (`daemon.py`
`_supervise_chrome` documents the handoff and infers liveness from CDP because of
it). The session kill then reports success while Chrome keeps running and keeps
this cluster's CDP port, so the next launch meets the daemon's port guard and
refuses. Observed on the `win` runner 2026-07-28, cleared by hand.

A better tree walk cannot fix this. `shared.proc.kill_process_tree` resolves
descendants by ppid, and the ppid link to the session is exactly what the handoff
destroyed — the orphan is not a descendant of anything we hold. What is missing is
a way to *name* Ava's Chrome without walking to it. That is this module: naming it
by the cluster's own profile directory, and only then handing the named root to
`kill_process_tree` (which does the right thing once the root is correct, since
Chrome's renderers/GPU/utility children really are its descendants).

## The identification, and why it cannot take the operator's browser

`is_cluster_chrome` selects a process only when ALL of these hold:

1. its argv carries the exact token ``--user-data-dir=<this cluster's profile>``
   (`profile.profile_dir()`, i.e. `$AVA_HOME/chrome-profile`);
2. its argv carries no ``--type=`` — Chrome tags every child process it spawns
   with one, so this narrows the match to the *browser* process and leaves the
   helpers to be reaped as its descendants;
3. its executable name looks like Chrome/Chromium (`_CHROME_NAME_MARKER`).

(1) is the authorizing signal and it is strictly positive. The profile path is
minted per cluster under a `$AVA_HOME` that belongs to this install alone, so no
Chrome that is not ours is launched with it: the operator's daily Chrome runs on
the platform-default profile and passes no `--user-data-dir` at all, and any other
explicitly-profiled Chrome on the box passes a different one (both shapes measured
side by side on the dev Mac — a daily Chrome with a flagless 60-character argv, and
a `chrome-devtools-mcp` Chrome on its own cache profile). The one process that
*can* satisfy (1) without being the daemon's own child is a Chrome someone started
by hand against Ava's profile — which is holding Ava's profile lock and Ava's CDP
port, is precisely the squatter the port guard complains about, and is a thing a
full teardown is right to take down.

(3) only narrows: it keeps a non-Chrome process that merely mentions the path on
its command line (a `grep`, an editor) out of the kill. Being a name test it can
be wrong in one direction only — an unrecognised Chrome build is not selected, so
the orphan survives, which is today's behaviour and not a new harm.

Nothing here is ever selected by exclusion, and no signal is inferred from the
absence of evidence. Unreadable argv (another user's process — `AccessDenied`) is
therefore a **skip, not an error**: the token is the only thing that authorizes a
kill, so a process whose argv cannot be read has not been identified, and the
teardown carries on. The asymmetry is deliberate — a missed orphan costs the
operator one manual kill, and a wrong kill costs them their logged-in browser.

## Platform

Both, unbranched. A `SingletonLock` handoff is Chrome's behaviour, not Windows'.
POSIX makes it rare — the daemon `os.execvp`s, so the pane's process *is* Chrome
and there is no launcher left to hand off from — but a Chrome that detaches from
that exec'd process leaves the identical orphan on the identical CDP port, and
nothing in the identification is platform-specific. `psutil` supplies argv on
both, so there is no reason to make POSIX carry the known gap Windows just closed.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import psutil

from shared.log import logger
from shared.proc import kill_process_tree

from .profile import profile_dir

# psutil exceptions meaning "gone, or not ours to read". A process we cannot read
# is one we cannot identify, so every one of these is a skip (module docstring).
_UNREADABLE = (psutil.NoSuchProcess, psutil.AccessDenied, OSError)

_USER_DATA_DIR = "--user-data-dir="
# Chrome stamps every process it spawns for itself with `--type=<renderer|gpu-process
# |utility|zygote|…>`; the browser process alone has none. Verified on the dev Mac:
# the browser process carries `--user-data-dir` and no `--type`, its GPU helper
# carries both.
_CHILD_TYPE = "--type="
# Substring, not an exact set: the browser process is `chrome.exe` on Windows,
# `Google Chrome` on macOS, and `chrome` / `google-chrome-stable` / `chromium` /
# `chromium-browser` on Linux. Matching loosely here is safe because this test only
# narrows — the profile token is what authorizes the kill.
_CHROME_NAME_MARKER = "chrom"


def is_cluster_chrome(name: str, cmdline: Sequence[str] | None, profile: Path) -> bool:
    """True when a process is *this cluster's* headed Chrome browser process.

    The three conjuncts and the argument that none of them can select a Chrome
    that is not ours are in the module docstring. `cmdline=None` means the argv
    could not be read and returns False — unidentified is not a licence to kill.

    Pure and total on fabricated inputs, so the refusal cases (a Chrome on another
    profile, a Chrome with no `--user-data-dir`, a Chrome whose argv is
    unreadable) are asserted directly rather than through a process table.
    """
    if cmdline is None:
        return False
    if _CHROME_NAME_MARKER not in name.lower():
        return False
    if any(arg.startswith(_CHILD_TYPE) for arg in cmdline):
        return False  # a helper Chrome spawned for itself; reaped as a descendant
    for arg in cmdline:
        if not arg.startswith(_USER_DATA_DIR):
            continue
        # Compare as paths, not strings: `Path` folds a trailing separator away,
        # and on Windows it compares case-insensitively — both of which a literal
        # string match would get wrong in the false-negative direction.
        if Path(arg[len(_USER_DATA_DIR) :]) == profile:
            return True
    # Chrome also accepts the flag split across two argv entries
    # (`--user-data-dir <path>`), and a Chrome that relaunches itself (crash
    # restore, auto-update) has been observed re-tokenizing the original single
    # token. The two-token form must identify too, or such a Chrome — ours, on
    # our profile, holding our port — reads as foreign.
    for i, arg in enumerate(cmdline[:-1]):
        if arg == _USER_DATA_DIR[:-1] and Path(cmdline[i + 1]) == profile:
            return True
    return False


def _readable_facts(proc: psutil.Process) -> tuple[str, list[str] | None]:
    """`(name, argv)` for `proc`, with argv None when it cannot be read."""
    try:
        return proc.name(), proc.cmdline()
    except _UNREADABLE:
        return "", None


def find_cluster_chrome(profile: Path | None = None) -> list[int]:
    """Pids of this cluster's Chrome browser processes (usually zero or one).

    Walks the whole process table because the orphan is reachable no other way —
    it is a descendant of nothing we hold. One pass, argv already batched by
    `process_iter`; a process that exits mid-walk is skipped like any other
    unreadable one.
    """
    target = profile_dir() if profile is None else profile
    found: list[int] = []
    for proc in psutil.process_iter():
        name, cmdline = _readable_facts(proc)
        if is_cluster_chrome(name, cmdline, target):
            found.append(proc.pid)
    return found


def reap_cluster_chrome() -> list[int]:
    """Kill any Chrome running on this cluster's profile. Returns the pids killed.

    Two callers:

    - the teardown paths that explicitly asked for the browser down
      (`ava stop --stop-browser`, `ava cluster destroy`), and only after the
      `ava-browser` session and the watchdog are already dead — a live watchdog
      would relaunch Chrome straight back onto the port we just cleared. It does
      not fight the daemon's port guard either: no launch is in flight during a
      teardown, and the guard's whole complaint is the Chrome this removes.
    - the browser healthcheck's unsupervised-Chrome branch
      (`services/healthchecks/browser.py`), which sweeps an orphan of ours that
      holds the CDP port while the session is gone, then respawns the session in
      the same round — the operator remedy, automated. There the watchdog IS the
      caller and the respawn is the point.

    A no-op in the overwhelmingly common case: with nothing to reap it costs one
    process-table pass, kills nothing, logs nothing, and returns `[]`. Idempotent
    for the same reason — a second call finds nothing, and `kill_process_tree` is
    itself a no-op on a pid that has already gone.
    """
    pids = find_cluster_chrome()
    if not pids:
        return []
    logger.info(
        "reaping {n} Chrome process tree(s) on this cluster's profile {profile}: {pids} "
        "(left outside the ava-browser session by a SingletonLock handoff)",
        n=len(pids),
        profile=profile_dir(),
        pids=pids,
    )
    for pid in pids:
        kill_process_tree(pid)
    return pids
