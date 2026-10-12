"""Typed payload carried by the existing local deploy-pause owner journal."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import cast


class MaintenancePhase(StrEnum):
    """The closed lifecycle vocabulary persisted in the local hold journal."""

    PREPARING = "preparing"
    DRAINING = "draining"
    DRAINED = "drained"
    STOPPING = "stopping"
    STOPPED = "stopped"


# From `drained` on the drain is certified: that transition required every
# member drained (base.deploy.maintenance.admission.set_phase).
CERTIFIED_PHASES = frozenset(
    {
        MaintenancePhase.DRAINED,
        MaintenancePhase.STOPPING,
        MaintenancePhase.STOPPED,
    }
)


@dataclass(frozen=True)
class MaintenanceHold:
    phase: MaintenancePhase = MaintenancePhase.PREPARING
    # The restart command remains in Postgres across the data-plane move.
    # A zero value means preparation has not yet durably enqueued it.
    commands: dict[int, int] = field(default_factory=dict[int, int])
    drained: tuple[int, ...] = ()
    # Existing unowned idle intent stays untouched; it is not a restart request.
    parked: tuple[int, ...] = ()

    def encode(self) -> dict[str, object]:
        return {
            "phase": self.phase.value,
            "commands": {str(agent): command for agent, command in self.commands.items()},
            "drained": list(self.drained),
            "parked": list(self.parked),
        }

    @classmethod
    def decode(cls, value: object) -> "MaintenanceHold":
        if not isinstance(value, dict):
            raise TypeError("maintenance must be an object")
        raw = cast(dict[str, object], value)
        phase, commands, drained = raw["phase"], raw["commands"], raw["drained"]
        if (
            not isinstance(phase, str)
            or not isinstance(commands, dict)
            or not isinstance(drained, list)
        ):
            raise ValueError("invalid maintenance phase or resume cohort")  # noqa: TRY004
        try:
            parsed_phase = MaintenancePhase(phase)
        except ValueError as exc:
            raise ValueError("invalid maintenance phase") from exc
        parsed = _commands(cast(dict[object, object], commands))
        receipts = cast(list[object], drained)
        if any(type(agent) is not int or agent not in parsed for agent in receipts):
            raise ValueError("maintenance receipt is outside the resume cohort")
        if len(set(receipts)) != len(receipts):
            raise ValueError("duplicate maintenance receipt")
        parked_ids = _parked_ids(raw["parked"], parsed)
        return cls(
            parsed_phase,
            parsed,
            tuple(cast(list[int], receipts)),
            tuple(cast(list[int], parked_ids)),
        )


def _parked_ids(parked: object, cohort: dict[int, int]) -> list[object]:
    if not isinstance(parked, list):
        raise TypeError("parked agents must be a list")
    parked_ids = cast(list[object], parked)
    if any(type(agent) is not int or agent < 1 or agent in cohort for agent in parked_ids):
        raise ValueError("invalid parked agent IDs")
    if len(set(parked_ids)) != len(parked_ids):
        raise ValueError("duplicate parked agent ID")
    return parked_ids


def _commands(commands: dict[object, object]) -> dict[int, int]:
    parsed: dict[int, int] = {}
    for agent, command in commands.items():
        if not isinstance(agent, str) or not agent.isdecimal() or int(agent) < 1:
            raise ValueError("maintenance agent IDs must be positive integers")
        if type(command) is not int or command < 0:
            raise ValueError("maintenance restart IDs must be nonnegative integers")
        parsed[int(agent)] = command
    return parsed
