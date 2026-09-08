"""Typed payload carried by the existing local deploy-pause owner journal."""

import datetime as dt
from dataclasses import dataclass, field
from typing import Literal, cast

MaintenancePhase = Literal[
    "preparing", "draining", "drained", "stopping", "stopped", "starting", "ready"
]
_PHASES = ("preparing", "draining", "drained", "stopping", "stopped", "starting", "ready")

_REPAIR_RECORD_KEYS = ("at", "by", "user", "uid", "pid", "parent", "machine")


@dataclass(frozen=True)
class MaintenanceHold:
    phase: MaintenancePhase = "preparing"
    # The restart command remains in Postgres across the data-plane move.
    # A zero value means preparation has not yet durably enqueued it.
    commands: dict[int, int] = field(default_factory=dict[int, int])
    drained: tuple[int, ...] = ()
    failures: dict[int, str] = field(default_factory=dict[int, str])
    # Crash-equivalent receipts: the turn raised a database-outage exception,
    # so the continuation outcome is unknown but durable (the restart pointer
    # survives exactly as after a host crash). Recorded for audit; never
    # blocks resume. The host re-drives the held-control path on the next wake.
    undelivered: dict[int, str] = field(default_factory=dict[int, str])
    # Failures an operator cleared through `ava maintenance repair`, verbatim
    # copies of the cleared `failures` entries, kept as the journal's audit
    # record of that repair (the "before" side of the CAS).
    repaired: dict[int, str] = field(default_factory=dict[int, str])
    # Who performed the repair, when, and from which process. None while no
    # repair happened. Keys are validated on decode; values are the
    # operator-identity facts that make a repair "sanctioned".
    repair_record: dict[str, str] | None = None
    # Existing unowned idle intent stays untouched; it is not a restart request.
    parked: tuple[int, ...] = ()

    def encode(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "commands": {str(agent): command for agent, command in self.commands.items()},
            "drained": list(self.drained),
            "failures": {str(agent): reason for agent, reason in self.failures.items()},
            "undelivered": {str(agent): reason for agent, reason in self.undelivered.items()},
            "repaired": {str(agent): reason for agent, reason in self.repaired.items()},
            "repair_record": self.repair_record,
            "parked": list(self.parked),
        }

    @classmethod
    def decode(cls, value: object) -> "MaintenanceHold":
        if not isinstance(value, dict):
            raise TypeError("maintenance must be an object")
        raw = cast(dict[str, object], value)
        phase, commands, drained = raw["phase"], raw["commands"], raw["drained"]
        if phase not in _PHASES or not isinstance(commands, dict) or not isinstance(drained, list):
            raise ValueError("invalid maintenance phase or resume cohort")
        parsed = _commands(cast(dict[object, object], commands))
        receipts = cast(list[object], drained)
        if any(type(agent) is not int or agent not in parsed for agent in receipts):
            raise ValueError("maintenance receipt is outside the resume cohort")
        if len(set(receipts)) != len(receipts):
            raise ValueError("duplicate maintenance receipt")
        failed = _receipts(raw["failures"], "maintenance failures")
        undelivered = _receipts(raw.get("undelivered", {}), "undelivered receipts")
        repaired = _receipts(raw.get("repaired", {}), "repaired receipts")
        repair_record = _repair_record(raw.get("repair_record"))
        parked = raw["parked"]
        if not isinstance(parked, list):
            raise TypeError("parked agents must be a list")
        parked_ids = cast(list[object], parked)
        if any(type(agent) is not int or agent < 1 or agent in parsed for agent in parked_ids):
            raise ValueError("invalid parked agent IDs")
        if len(set(parked_ids)) != len(parked_ids):
            raise ValueError("duplicate parked agent ID")
        return cls(
            phase,
            parsed,
            tuple(cast(list[int], receipts)),
            failed,
            undelivered,
            repaired,
            repair_record,
            tuple(cast(list[int], parked_ids)),
        )


def _commands(commands: dict[object, object]) -> dict[int, int]:
    parsed: dict[int, int] = {}
    for agent, command in commands.items():
        if not isinstance(agent, str) or not agent.isdecimal() or int(agent) < 1:
            raise ValueError("maintenance agent IDs must be positive integers")
        if type(command) is not int or command < 0:
            raise ValueError("maintenance restart IDs must be nonnegative integers")
        parsed[int(agent)] = command
    return parsed


def _receipts(receipts: object, label: str) -> dict[int, str]:
    if not isinstance(receipts, dict):
        raise TypeError(f"{label} must be an object")
    parsed: dict[int, str] = {}
    for agent, reason in cast(dict[object, object], receipts).items():
        if not isinstance(agent, str) or not agent.isdecimal() or int(agent) < 1:
            raise ValueError(f"{label} must name a positive agent ID")
        if not isinstance(reason, str) or not reason or len(reason) > 100:
            raise ValueError(f"invalid {label} category")
        parsed[int(agent)] = reason
    return parsed


def validate_repair_record(record: object) -> dict[str, str]:
    """Validate an operator repair record before it is CASed into the journal."""
    parsed = _repair_record(record)
    if parsed is None:
        raise ValueError("maintenance repair record is required")
    return parsed


def _repair_record(record: object) -> dict[str, str] | None:
    if record is None:
        return None
    if not isinstance(record, dict):
        raise TypeError("maintenance repair record must be an object")
    parsed: dict[str, str] = {}
    for key, value in cast(dict[object, object], record).items():
        if not isinstance(key, str) or key not in _REPAIR_RECORD_KEYS:
            raise ValueError("invalid maintenance repair record key")
        if not isinstance(value, str) or not value or len(value) > 200:
            raise ValueError("invalid maintenance repair record value")
        parsed[key] = value
    if "at" not in parsed:
        raise ValueError("maintenance repair record must carry a timestamp")
    try:
        dt.datetime.fromisoformat(parsed["at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("maintenance repair timestamp must be ISO-8601") from exc
    return parsed
