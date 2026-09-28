"""The fleet cutover's maintenance hold, which only the cutover flow releases.

`scripts/cutover_adopt_home.py` records the hold it adopts (a completed legacy
stop's) or creates in its journal, `ADOPTION_JOURNAL`. While exactly that hold
stands, an ordinary start never releases it: not a typed `ava start`, and not
the autostart job after a reboot. Business opens only at the go/no-go gate,
through the `ava maintenance resume` command the held first start prints.

- Before the held first start (phase `stopped`) an ordinary start refuses and
  names `cutover_adopt_home.py --start`. On a gateway that start follows the
  data-plane cutover and the database-records repair.
- After it (phase `starting` or `ready`) an ordinary start brings the unit up
  and keeps the hold.

Any other hold, including a later stop's on an adopted home, keeps the
ordinary release. Deleted with the `scripts/cutover_*` scripts after the
cutover.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ADOPTION_JOURNAL = "cutover-rollback/adopt-home.json"


@dataclass(frozen=True)
class CutoverHold:
    """The `(holder, acquired_at)` generation the adoption journal recorded."""

    holder: str
    acquired_at: datetime

    def resume_command(self) -> str:
        at = self.acquired_at.isoformat()
        return f"ava maintenance resume --operation {self.holder} --acquired-at {at}"


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


def start_instruction(home: Path) -> str:
    """How an operator starts `home` after its data-plane conversion."""
    if standing_hold(home) is None:
        return "`ava start`"
    return (
        "the database-records repair (`scripts/cutover_db_records.py`), then the held "
        f"first start {held_start_command(home)}; never bare `ava start` while the "
        "cutover hold stands"
    )
