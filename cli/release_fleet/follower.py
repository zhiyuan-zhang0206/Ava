"""A remote unit's executor: it follows the coordinator's instructions."""

from __future__ import annotations

from cli.release_fleet.inventory import NETWORKED_REFUSAL
from cli.release_transition.journal import Journal


def run_follower(journal: Journal) -> None:
    del journal
    raise RuntimeError(NETWORKED_REFUSAL)
