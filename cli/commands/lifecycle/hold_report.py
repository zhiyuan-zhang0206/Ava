"""This unit's maintenance hold as `ava status` reports it.

One local read of the hold journal, settings-lite: no database, gateway or service
probe, so it answers when everything else is down. `ava status` prints it as the
first section; `ava status --json` prints only it, for the fleet update's
start-of-work refusal and the out-of-band triage.
"""

from __future__ import annotations

import json

from base.deploy.maintenance import hold_driver, pause_owner

_SCOPE = "local unit; excludes independent OS-managed extras and remote hosts"


def _driver_evidence(driver: hold_driver.HoldDriver | None) -> dict[str, object] | None:
    """The recorded shepherd identity, for humans reading the journal.

    `root` is the process the stranded-hold verdict judges; `leader` (the session
    leader at mint time) is display evidence only. Liveness is probed from local
    process state (pid + birth). None when no identity was recorded (a
    daemon-driven pause).
    """
    if driver is None:
        return None
    return {
        "liveness": hold_driver.liveness(driver),
        "root": _process_ref(driver.root),
        "leader": _process_ref(driver.leader),
    }


def _process_ref(ref: hold_driver.ProcessRef | None) -> dict[str, object] | None:
    """One recorded process reference as the journal reader needs it: pid + argv."""
    if ref is None:
        return None
    return {"pid": ref.pid, "argv": ref.argv}


def hold_report() -> dict[str, object]:
    """The journal's hold: status, generation, phase payload and the shepherd's liveness."""
    current = pause_owner.read()
    return {
        "status": current.status,
        "operation": current.holder,
        "acquired_at": current.acquired_at.isoformat() if current.acquired_at else None,
        "maintenance": current.maintenance.encode() if current.maintenance else None,
        "driver": _driver_evidence(current.driver),
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
    liveness = hold_driver.liveness(current.driver) if current.driver else "missing"
    acquired_at = current.acquired_at.isoformat() if current.acquired_at else None
    print(
        f"maintenance hold: {hold.phase}  operation={current.holder}  acquired_at={acquired_at}  "
        f"driver={liveness}  failures={sorted(hold.failures) or 'none'}"
    )
    print("  `ava start` re-delivers any failed continuation and releases the hold.")


def cmd_status_json() -> int:
    """`ava status --json`: the hold, as the one JSON object on stdout."""
    print(json.dumps({"hold": hold_report()}, sort_keys=True))
    return 0
