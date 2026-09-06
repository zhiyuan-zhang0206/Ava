"""Typed payload carried by the existing local deploy-pause owner journal."""

from dataclasses import dataclass, field
from typing import Literal, cast

MaintenancePhase = Literal[
    "preparing", "draining", "drained", "stopping", "stopped", "starting", "ready"
]
_PHASES = ("preparing", "draining", "drained", "stopping", "stopped", "starting", "ready")


@dataclass(frozen=True)
class MaintenanceHold:
    phase: MaintenancePhase = "preparing"
    # The restart command remains in Postgres across the data-plane move.
    # A zero value means preparation has not yet durably enqueued it.
    commands: dict[int, int] = field(default_factory=dict[int, int])
    drained: tuple[int, ...] = ()
    failures: dict[int, str] = field(default_factory=dict[int, str])
    # Existing unowned idle intent stays untouched; it is not a restart request.
    parked: tuple[int, ...] = ()

    def encode(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "commands": {str(agent): command for agent, command in self.commands.items()},
            "drained": list(self.drained),
            "failures": {str(agent): reason for agent, reason in self.failures.items()},
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
        failed = _failures(raw["failures"])
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


def _failures(failures: object) -> dict[int, str]:
    if not isinstance(failures, dict):
        raise TypeError("maintenance failures must be an object")
    failed: dict[int, str] = {}
    for agent, reason in cast(dict[object, object], failures).items():
        if not isinstance(agent, str) or not agent.isdecimal() or int(agent) < 1:
            raise ValueError("maintenance failure must name a positive agent ID")
        if not isinstance(reason, str) or not reason or len(reason) > 100:
            raise ValueError("invalid maintenance failure category")
        failed[int(agent)] = reason
    return failed
