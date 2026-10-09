"""What the native runtime remembers about each agent's bound impersonation relay.

The agent host builds one `RelaySupervision` and holds it: it hands it to the turns it runs
(`AvaContext.relays`, read by the claim gate) and to its own supervision and activation calls
(`agent.impersonation`). The agent-host service drives several agents in one process, so every
record is keyed by agent; entries die with the process or are popped when the native agent
regains control.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import psutil

from base.native_process.ownership import OwnedProcess


class RelayChild:
    """The native runtime's bound relay for one lease, spawned at activation."""

    __slots__ = ("generation", "lease_id", "process", "spawned_at", "token")

    def __init__(
        self,
        lease_id: str,
        process: subprocess.Popen[bytes],
        token: str,
        spawned_at: float,
        generation: int = 0,
    ) -> None:
        self.generation = generation
        self.lease_id = lease_id
        self.process = process
        self.token = token
        self.spawned_at = spawned_at


class RelaySupervision:
    """The known child handles, fenced by lease and transport generation."""

    def __init__(self) -> None:
        self.children: dict[int, RelayChild] = {}

    def drop(self, agent_id: int) -> None:
        """Forget the handle; a terminal lease revokes the child independently."""
        self.children.pop(agent_id, None)


def relay_exited(
    child: RelayChild | None, identity: Mapping[str, Any] | None, *, provider: str = "codex"
) -> bool:
    """Read known child/birth evidence; alive, missing and unknown are not exit.

    A reused PID means the recorded sender exited, never that its replacement
    may be signaled. This observation does not retire or authorize a sender.
    """
    if provider != "codex":
        return False
    if child is not None:
        return child.process.poll() is not None
    if identity is None:
        return False
    process = OwnedProcess(identity["pid"], identity["birth"], identity["starttime"])
    try:
        return not process.live()
    except (psutil.Error, OSError, RuntimeError):
        return False


def terminate_relay(child: RelayChild) -> None:
    """Stop the bound relay with its existing graceful wait and kill fallback."""
    process = child.process
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def heartbeat_fresh(heartbeat: datetime | None, *, now: datetime | None = None) -> bool:
    """Compare the relay heartbeat with the native supervision freshness window."""
    from base.agents.impersonation import RELAY_HEARTBEAT_STALE_SECONDS

    current = now or datetime.now(UTC)
    if heartbeat is None:
        return False
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=UTC)
    return heartbeat >= current - timedelta(seconds=RELAY_HEARTBEAT_STALE_SECONDS)
