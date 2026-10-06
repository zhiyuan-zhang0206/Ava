"""The service's ledger of live shell identities, and the sweep of a crashed service's leftovers.

The service is the only writer of ``$AVA_HOME/run/pty-sessions.json``: every
live session's shell identity and any foreground leader explicitly captured at
close or kill, rewritten atomically as sessions come and go. It
exists for one reader, the next service start (or a stop that finds the service
gone): a service that died uncleanly closed its masters. Hangup may end foreground
work, but ignored hangup and background jobs can outlive it.
The sweep attempts closure only for the identities the ledger names, each verified by
birth before any signal, through the one terminal closure, and reports the busy
ones it closed so their owners are told (`ops/pty_close_notices.py`).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

from base.host.atomic_io import write_text_atomic
from base.log import logger
from base.native_process.ownership import OwnedProcess
from base.sessions.pty import closure

# A crashed service's leftovers are already hung up; the grace only lets a job
# that handles TERM clean up before the SIGKILL.
SWEEP_HANGUP_WAIT_S = 1.0
SWEEP_KILL_S = 2.0

_VERSION = 1


def write(path: Path, targets: Iterable[closure.Target]) -> None:
    """Persist the live sessions' identities (atomic replace, owner-only)."""
    payload = {
        "version": _VERSION,
        "sessions": {
            target.name: {
                "shell": _wire(target.shell),
                "members": [_wire(member) for member in target.members],
            }
            for target in sorted(targets, key=lambda target: target.name)
        },
    }
    write_text_atomic(path, json.dumps(payload), mode=0o600, sync_file=False)


def _wire(identity: OwnedProcess) -> dict[str, Any]:
    return {"pid": identity.pid, "birth": identity.birth, "starttime": identity.starttime}


def read(path: Path) -> list[closure.Target]:
    """The recorded sessions; empty when the ledger is absent.

    An unreadable ledger is logged and reads empty: the sweep can then only
    miss leftovers it was never told about, never signal a guessed process.
    """
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        logger.warning(
            "pty ledger {path} is unreadable ({exc}); nothing to sweep", path=path, exc=exc
        )
        return []
    try:
        sessions = cast("dict[str, dict[str, Any]]", raw["sessions"])
        return [
            closure.Target(
                name,
                closure.identity_from_wire(item["shell"]),
                tuple(closure.identity_from_wire(member) for member in item["members"]),
            )
            for name, item in sessions.items()
        ]
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "pty ledger {path} is malformed ({exc}); nothing to sweep", path=path, exc=exc
        )
        return []


def leftovers(path: Path) -> list[str]:
    """Recorded live shells, by name; known job survivors are diagnostics only."""
    return sorted(target.name for target in read(path) if _alive(target.shell))


def _has_live_process(target: closure.Target) -> bool:
    return any(_alive(identity) for identity in (target.shell, *target.members))


def sweep(path: Path) -> closure.Outcome:
    """Attempt closure of the recorded known identities, retaining live shells for retry.

    A still-live recorded shell and its known groups are attempted; when the shell
    is gone, only its recorded members are attempted. Known job-only survivors are
    diagnostic evidence, not a terminal-presence gate, so they do not retain a
    ledger entry. The outcome also names recorded busy sessions whose shells the
    crash already ended, so their owners can be notified without claiming that
    unrecorded descendants disappeared.
    """
    recorded = read(path)
    targets = [target for target in recorded if _has_live_process(target)]
    outcome = closure.Outcome()
    if targets:
        logger.warning(
            "pty sweep: closing {count} session(s) a previous service left running: {names}",
            count=len(targets),
            names=sorted(target.name for target in targets),
        )
        outcome = closure.close_sessions(targets, grace_s=SWEEP_HANGUP_WAIT_S, kill_s=SWEEP_KILL_S)
    write(path, (target for target in recorded if _alive(target.shell)))
    return _with_ended_busy(outcome, recorded)


def _with_ended_busy(outcome: closure.Outcome, recorded: list[closure.Target]) -> closure.Outcome:
    """Add the recorded busy sessions whose shell is gone and that the closure did not report."""
    reported = {closed.name for closed in outcome.closed}
    ended = tuple(
        closure.ClosedSession(target.name, target.shell)
        for target in recorded
        if target.name not in reported and _was_busy(target) and not _alive(target.shell)
    )
    return closure.Outcome(outcome.closed + ended, outcome.survivors)


def _was_busy(target: closure.Target) -> bool:
    """The ledger records a known foreground target besides its shell."""
    return any(member != target.shell for member in target.members)


def _alive(identity: OwnedProcess) -> bool:
    try:
        return identity.live()
    except RuntimeError:
        return False  # an identity that cannot be verified is never signalled
