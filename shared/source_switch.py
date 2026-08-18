"""Local marker for the update's source-switch critical section.

`ava cluster update` switches the checkout's source tree with a `git checkout`
that writes files one at a time. While the switch is in flight, a process that
imports from the tree can read a half-written or mixed tree — an import crash,
or worse a launch whose command shape was decided by a torn read. The existing
guards bound the damage on the update's own side (`verify_tree_at` refuses to
start services on a mixed tree), but the healthcheck respawn path — the old
watchdog reviving a dead service *while* the checkout runs — has no such gate.

This module is that gate: the update marks the tree as being switched, the
respawn path holds back while the marker is fresh, and the update's own
`ava start` (which runs only on the verified, complete tree) relaunches
everything. The marker is a local file with a TTL — a crashed update leaves it
to expire, so a stuck marker can never hold respawns back forever (the
watchdog-probe is additionally exempt by contract: its whole job is dumb
revival, see `cli/commands/_cluster_watchdog_probe.py`).

Lifecycle, both update legs:
- in-process (`_run_agent_runner_self_update_inner`): `mark_switching()` before
  the checkout, `clear_switching()` in the surrounding finally;
- Windows cmd.exe ladder (`ops.cluster_deploy.spawn_update`): an `on` / `off`
  CLI seam (`cli/commands/_source_switch_marker.py`) at the chain head / tail
  and in the abort arm, mirroring the updater lease's placement.
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path

from shared.paths import run_dir

# The marker's fresh window. The update's own stall bound is
# `NO_PROGRESS_TIMEOUT_S` (900s) and the checkout→start window is a few
# minutes, so 900s gives a slow host the whole window plus a generous tail,
# while a crashed update's stale marker stops holding respawns back after 15
# minutes (the watchdog-probe exemption covers the first of those minutes).
_SWITCH_TTL_S = 900.0

# One marker per cluster home, under the same run dir the session records use.
_MARKER_NAME = "source-switching"


def _marker_path() -> Path:
    return run_dir() / _MARKER_NAME


def mark_switching() -> None:
    """Record that the source tree is being switched (checkout in flight).

    Best-effort: an unwritable run dir must not abort an update — the marker
    only gates respawns, and the update's own start is never gated by it.
    """
    with contextlib.suppress(OSError):
        p = _marker_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(time.time()) + "\n")


def clear_switching() -> None:
    """End the switch window (the update finished, aborted, or a start ran).

    Best-effort, same rationale as `mark_switching`."""
    with contextlib.suppress(OSError):
        _marker_path().unlink(missing_ok=True)


def is_switching() -> bool:
    """True when a source switch is (recently) in flight — spawns should hold.

    A missing, unreadable, or expired marker reads as False: the gate fails
    open, because the marker is an advisory window and a stuck marker must not
    be able to stop revival forever.
    """
    try:
        ts = float(_marker_path().read_text().strip())
    except (OSError, ValueError):
        return False
    return time.time() - ts < _SWITCH_TTL_S
