"""The service's ledger of live shell identities, and the sweep of a crashed service's leftovers.

The service is the only writer of ``$AVA_HOME/run/pty-sessions.json``: every
live session's shell identity and the members of its session last seen alive,
rewritten atomically as sessions come and go and every `SNAPSHOT_INTERVAL_S`. It
exists for one reader, the next service start (or a stop that finds the service
gone): a service that died uncleanly closed its masters, which hangs up every
shell, but a shell that ignores the hangup, or a job that does, can outlive it.
The sweep closes exactly the identities the ledger names, each verified by
birth before any signal, through the one terminal closure.
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

# How often the service re-reads each session's membership into the ledger.
SNAPSHOT_INTERVAL_S = 10.0

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
    """The recorded sessions that still have a live process, by name.

    What a stop that finds the service gone must still close: the sessions whose
    shell, or a recorded member of it, outlived the service.
    """
    return sorted(target.name for target in read(path) if _has_live_process(target))


def _has_live_process(target: closure.Target) -> bool:
    return any(_alive(identity) for identity in (target.shell, *target.members))


def sweep(path: Path) -> closure.Outcome:
    """Close every recorded session that still has a live process, then clear the ledger.

    A shell that is still its recorded process is closed with its session; when
    it is gone (the master's hangup ended it) the recorded members that outlived
    it (a job that ignored the hangup) are closed. The returned outcome carries
    the busy sessions that were closed: the shape a caller turns into owner
    notices.
    """
    targets = [target for target in read(path) if _has_live_process(target)]
    outcome = closure.Outcome()
    if targets:
        logger.warning(
            "pty sweep: closing {count} session(s) a previous service left running: {names}",
            count=len(targets),
            names=sorted(target.name for target in targets),
        )
        outcome = closure.close_sessions(targets, grace_s=SWEEP_HANGUP_WAIT_S, kill_s=SWEEP_KILL_S)
    write(path, [])
    return outcome


def _alive(identity: OwnedProcess) -> bool:
    try:
        return identity.live()
    except RuntimeError:
        return False  # an identity that cannot be verified is never signalled
