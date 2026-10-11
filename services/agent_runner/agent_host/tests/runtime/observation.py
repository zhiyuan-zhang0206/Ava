"""The explicit context facts observed inside a hosted graph node task."""

from dataclasses import dataclass
from typing import Protocol

from base.cluster.machine import MachineIdentity


class ModelName(Protocol):
    name: str


class PublisherIdentity(Protocol):
    agent_id: int


@dataclass
class HostTurnObservation:
    agent_id: int | None
    model: str
    plugin_marker: str
    llm: ModelName
    publisher: PublisherIdentity
    ops_pool: object
    machine: MachineIdentity
