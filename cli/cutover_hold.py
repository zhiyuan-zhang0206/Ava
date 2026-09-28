"""The fleet cutover's maintenance hold, which only the cutover flow releases.

`scripts/cutover_adopt_home.py` records the hold it adopts (a completed legacy
stop's) or creates in its journal, `ADOPTION_JOURNAL`. While exactly that hold
stands, no ordinary path releases it: not a typed `ava start`, not the
autostart job after a reboot, not `ava cluster recover` and not
`ava maintenance resume`. Business opens only at the go/no-go gate, through
the script's `--resume` step (`release_command`), which checks the cutover's
own records first.

- Before the held first start (phase `stopped`) an ordinary start refuses and
  names `cutover_adopt_home.py --start`, and so does `ava maintenance start`
  with the hold's exact generation. On a gateway that start follows the
  data-plane cutover and the database-records repair.
- After it (phase `starting` or `ready`) an ordinary start brings the unit up
  and keeps the hold. One that passes readiness completes a `starting` hold (a
  failed or unready held first start) to `ready`, as the held first start does.

Any other hold, including a later stop's on an adopted home, keeps the
ordinary release. Deleted with the `scripts/cutover_*` scripts after the
cutover.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ADOPTION_JOURNAL = "cutover-rollback/adopt-home.json"
# The holder of a hold the cutover creates (`cutover:<id>`); no other hold takes it.
CUTOVER_HOLDER_PREFIX = "cutover:"


@dataclass(frozen=True)
class CutoverHold:
    """The `(holder, acquired_at)` generation the adoption journal recorded."""

    holder: str
    acquired_at: datetime


def recorded_hold(home: Path) -> CutoverHold | None:
    """The hold `home`'s adoption journal recorded; None when it was never adopted."""
    from shared.verified_file import regular_bytes

    path = home / ADOPTION_JOURNAL
    try:
        journal = json.loads(regular_bytes(path))
        named = Path(journal["home"])
        hold = CutoverHold(
            journal["hold"]["holder"], datetime.fromisoformat(journal["hold"]["acquired_at"])
        )
    except FileNotFoundError:
        return None
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"unreadable adoption journal {path}: {exc}") from exc
    if named.resolve() != home.resolve():
        raise RuntimeError(f"adoption journal {path} names another home: {named}")
    return hold


def standing_hold(home: Path) -> CutoverHold | None:
    """The recorded cutover hold, only while it is the hold standing on `home`."""
    from shared import pause_owner

    recorded = recorded_hold(home)
    if recorded is None:
        return None
    current = pause_owner.read_for_home(home)
    if current.status == "paused" and current.matches(recorded.holder, recorded.acquired_at):
        return recorded
    return None


def held_start_command(home: Path) -> str:
    return f"`.venv/bin/python scripts/cutover_adopt_home.py --home {home} --start`"


def release_command(home: Path) -> str:
    """The go/no-go step: the one exit of the cutover hold."""
    return f"`.venv/bin/python scripts/cutover_adopt_home.py --home {home} --resume`"


def resume_refusal(home: Path, holder: str, acquired_at: datetime) -> str | None:
    """Why `ava maintenance resume` must not release `(holder, acquired_at)`; None
    when it is not the recorded cutover hold.

    An unreadable journal cannot tell an adopted legacy stop's hold (named
    `local-pause:`) from a later stop's, so unlike an ordinary start, the
    exact-holder resume then proceeds and says so: with `ava maintenance start`,
    it is the way out of such a journal for a later hold
    (conventions/cutover-home-adoption.md). The journal is the cutover's alone,
    so a hold another subsystem took (`fleet:`, `pitr:`, `recovery:`) resumes
    the same way. Only a created cutover hold stays refused: it is
    `cutover:<id>`, a name no later hold takes, so damage to the journal opens
    no exit around the go/no-go step.
    """
    return _exact_holder_refusal(
        home,
        holder,
        acquired_at,
        unreadable=(
            f"a `{CUTOVER_HOLDER_PREFIX}` hold never resumes without it. Repair the "
            f"journal; the go/no-go gate releases the cutover hold with {release_command(home)}"
        ),
        proceeding=f"resuming {holder} as named",
        recorded=(
            f"{holder} is the fleet cutover's hold, which `ava maintenance resume` never "
            f"releases; the go/no-go gate releases it with {release_command(home)}"
        ),
    )


def start_refusal(home: Path, holder: str, acquired_at: datetime) -> str | None:
    """Why `ava maintenance start` must not start `(holder, acquired_at)` from
    phase `stopped`; None when it is not the recorded cutover hold.

    That start is the cutover's held first start, which on a gateway waits for
    the database-records repair (W7 before W8) and on a remote unit installs
    its capability bundle, so this verb refuses it as an ordinary start does.
    From phase `starting` on, it starts held as an ordinary start does. An
    unreadable journal is read as `resume_refusal` reads it: a later hold
    starts as named, a `cutover:<id>` hold stays refused.
    """
    return _exact_holder_refusal(
        home,
        holder,
        acquired_at,
        unreadable=(
            f"a `{CUTOVER_HOLDER_PREFIX}` hold never starts without it. Repair the journal; "
            f"its first start is {held_start_command(home)}"
        ),
        proceeding=f"starting {holder} as named",
        recorded=(
            f"{holder} is the fleet cutover's hold in phase stopped; its first start is "
            f"{held_start_command(home)} (a remote unit adds `--db-capability BUNDLE`), "
            "never `ava maintenance start`"
        ),
    )


def _exact_holder_refusal(
    home: Path,
    holder: str,
    acquired_at: datetime,
    *,
    unreadable: str,
    proceeding: str,
    recorded: str,
) -> str | None:
    """`recorded` when `(holder, acquired_at)` is the journal's cutover hold.

    An unreadable journal refuses a `cutover:<id>` holder with `unreadable`
    and lets any other holder through, printing `proceeding`.
    """
    try:
        journal = recorded_hold(home)
    except RuntimeError as exc:
        if holder.startswith(CUTOVER_HOLDER_PREFIX):
            return f"{exc}; {unreadable}"
        print(f"  ! {exc}; {proceeding}", file=sys.stderr)
        return None
    if journal is None or (journal.holder, journal.acquired_at) != (holder, acquired_at):
        return None
    return recorded


def start_instruction(home: Path) -> str:
    """How an operator starts `home` after its data-plane conversion."""
    if standing_hold(home) is None:
        return "`ava start`"
    return (
        "the database-records repair (`scripts/cutover_db_records.py`), then the held "
        f"first start {held_start_command(home)}; never bare `ava start` while the "
        "cutover hold stands"
    )
