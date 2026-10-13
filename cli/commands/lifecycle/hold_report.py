"""This unit's maintenance hold as `ava status` reports it.

One local read of the hold journal, settings-lite: no database, gateway or service
probe, so it answers when everything else is down. `ava status` prints it as the
first section; `ava status --json` prints only it, for the fleet update's
start-of-work refusal.
"""

from __future__ import annotations

import json

from base.deploy.maintenance import pause_owner

_SCOPE = "local unit; excludes independent OS-managed extras and remote hosts"


def hold_report() -> dict[str, object]:
    """The journal's hold: status, generation and phase payload."""
    current = pause_owner.read()
    return {
        "status": current.status,
        "operation": current.holder,
        "acquired_at": current.acquired_at.isoformat() if current.acquired_at else None,
        "maintenance": current.maintenance.encode() if current.maintenance else None,
        "scope": _SCOPE,
    }


def print_hold_section() -> None:
    """The human line(s) for `ava status`; a free unit prints one."""
    current = pause_owner.read()
    if current.status == "invalid":
        print(
            "maintenance hold: unreadable journal ($AVA_HOME/run/deploy-pause-owner.json); "
            "new work is refused until it is removed by hand (confirm no `ava stop` is in "
            "flight first), then `ava start`"
        )
        return
    hold = current.maintenance
    if current.status != "paused" or hold is None:
        print(f"maintenance hold: none ({current.status})")
        return
    acquired_at = current.acquired_at.isoformat() if current.acquired_at else None
    print(f"maintenance hold: {hold.phase}  operation={current.holder}  acquired_at={acquired_at}")
    print("  `ava start` releases the hold.")


def cmd_status_json() -> int:
    """`ava status --json`: the hold, as the one JSON object on stdout."""
    print(json.dumps({"hold": hold_report()}, sort_keys=True))
    return 0
