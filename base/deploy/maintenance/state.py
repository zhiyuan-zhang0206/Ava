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
    STARTING = "starting"
    READY = "ready"


# From `drained` on the drain is certified: that transition required every
# member drained with no failure (base.deploy.maintenance.admission.set_phase).
CERTIFIED_PHASES = frozenset(
    {
        MaintenancePhase.DRAINED,
        MaintenancePhase.STOPPING,
        MaintenancePhase.STOPPED,
        MaintenancePhase.STARTING,
        MaintenancePhase.READY,
    }
)


@dataclass(frozen=True)
class MaintenanceHold:
    phase: MaintenancePhase = MaintenancePhase.PREPARING
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
    # Existing unowned idle intent stays untouched; it is not a restart request.
    parked: tuple[int, ...] = ()

    def outside_cohort(self, agent_id: int) -> bool:
        """Whether `agent_id` provably has no continuation in this hold.

        Preparation captures every non-terminated agent of this machine under
        row locks, each as a restart command or parked. An agent outside that
        set is another machine's (every runner sees every wake), or one this
        hold never drains; no receipt of it gates the hold. Before the capture
        (phase `preparing`, empty cohort) nothing is proven.
        """
        captured = self.phase != MaintenancePhase.PREPARING or bool(self.commands or self.parked)
        return captured and agent_id not in self.commands and agent_id not in self.parked

    def settled_after_drain(self, agent_id: int) -> bool:
        """Whether the drain is certified and `agent_id` is a member with nothing
        left to continue: it drained, or it is parked (never drains).

        A wake of either under the hold claims nothing (no restart command is
        pending), so no receipt of it gates the hold. Until `drained` their
        failures still record.
        """
        return self.phase in CERTIFIED_PHASES and (
            agent_id in self.drained or agent_id in self.parked
        )

    def encode(self) -> dict[str, object]:
        return {
            "phase": self.phase.value,
            "commands": {str(agent): command for agent, command in self.commands.items()},
            "drained": list(self.drained),
            "failures": {str(agent): reason for agent, reason in self.failures.items()},
            "undelivered": {str(agent): reason for agent, reason in self.undelivered.items()},
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
        failed = _receipts(raw["failures"], "maintenance failures")
        undelivered = _receipts(raw.get("undelivered", {}), "undelivered receipts")
        parked_ids = _parked_ids(raw["parked"], parsed)
        return cls(
            parsed_phase,
            parsed,
            tuple(cast(list[int], receipts)),
            failed,
            undelivered,
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
