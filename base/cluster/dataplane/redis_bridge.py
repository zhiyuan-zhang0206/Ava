"""Where the macOS Redis private-network bridge listens: one answer for roster, probe and status.

Redis on macOS stays bound to loopback. A gateway whose machine declares a
reachable non-loopback address and whose data plane is local serves the cluster's
off-box Redis clients through a relay on that address (`services/redis_bridge`).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from base.cluster.record import get_record
from base.host.net.predicates import is_loopback_host


class RedisBridgeEndpoint(NamedTuple):
    listen_host: str
    port: int


class RedisBridgeUnconfiguredError(RuntimeError):
    """The bridge is required here but the home has no cluster record to name its port."""


def redis_bridge_endpoint(
    home: Path,
    *,
    machine_host: Callable[[], str],
    remote_data_plane: Callable[[], bool],
    platform: str | None = None,
) -> RedisBridgeEndpoint | None:
    """The relay's listen address, or None when this host needs no relay.

    Raises:
        RedisBridgeUnconfiguredError: the relay is needed but `home` has no cluster record.
    """
    if (platform or sys.platform) != "darwin" or remote_data_plane():
        return None
    listen_host = machine_host()
    if is_loopback_host(listen_host):
        return None
    record = get_record(home)
    if record is None:
        raise RedisBridgeUnconfiguredError(f"bridge unconfigured: no registry record for {home}")
    return RedisBridgeEndpoint(listen_host=listen_host, port=record.ports["redis"])
