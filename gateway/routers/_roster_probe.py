"""Bounded transport policy for the status roster's runner probes."""

from __future__ import annotations

import asyncio
from typing import Any

from ops import cluster_rpc
from shared.config import settings


async def dispatch_status_probe(name: str) -> dict[str, Any]:
    """Retry one fast transport failure inside the existing total deadline.

    The outer deadline is load-bearing: ``cluster_rpc`` applies ``timeout_s``
    per attempt, so retrying without it could double an 8-second roster budget
    for a blackholed host.
    """
    timeout_s = settings.gateway.status_probe_timeout_seconds
    try:
        async with asyncio.timeout(timeout_s):
            return await cluster_rpc.dispatch_to_machine(
                target_machine=name,
                kind="status_probe",
                payload={},
                timeout_s=timeout_s,
                retries=1,
            )
    except TimeoutError as exc:
        raise cluster_rpc.ClusterOpUnreachable(
            f"status_probe for machine={name!r} exceeded its {timeout_s:.1f}s total budget"
        ) from exc
