"""Bounded transport policy for a roster dial, and the identity-mismatch log.

A dial happens only on an explicit fresh read, or for a machine the heartbeat
service's snapshot (`gateway/cluster/snapshots.py`) does not cover, so the gateway
keeps no failure memory and runs no recovery dial of its own. Split out of
``status.py`` (which sits at the 800-line hard ceiling).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from base.config import settings
from ops import cluster_rpc

_log = logging.getLogger(__name__)


async def dispatch_status_probe(
    name: str, ops_url: str, *, timeout_s: float | None = None
) -> dict[str, Any]:
    """Retry one fast transport failure inside the existing total deadline.

    The outer deadline is load-bearing: ``cluster_rpc`` applies ``timeout_s``
    per attempt, so retrying without it could double an 8-second roster budget
    for a blackholed host.

    ``timeout_s`` defaults to the full per-machine budget
    (``settings.gateway.status_probe_timeout_seconds``).
    """
    if timeout_s is None:
        timeout_s = settings.gateway.status_probe_timeout_seconds
    try:
        async with asyncio.timeout(timeout_s):
            return await cluster_rpc.dispatch_to_url(
                target_machine=name,
                kind="status_probe",
                payload={},
                timeout_s=timeout_s,
                ops_url=ops_url,
                retries=1,
            )
    except TimeoutError as exc:
        raise cluster_rpc.ClusterOpUnreachable(
            f"status_probe for machine={name!r} exceeded its {timeout_s:.1f}s total budget"
        ) from exc


# Identity-mismatch episode tracking: one log line per mismatching episode, not
# one per panel poll. A stopped row's stale URL answering as a different host is
# the expected face of the stop (its address was handed on); an active row's
# mismatch is the real misregistration signal (2026-07-18 / 2026-08-30). The
# episode ends once a probe's identity echoes correctly again, so a later
# mismatch logs anew. Process-local like the probe backoff: a gateway restart
# just re-logs once.
_identity_mismatch_active: set[str] = set()


def log_identity_mismatch(
    name: str, gateway_url: str | None, responder: str, *, stopped: bool
) -> None:
    """Log one identity-mismatch sighting, deduped to once per episode.

    `stopped` (the machines row carries a stop marker) downgrades the line to
    INFO and says why the mismatch is expected — reported once, not at panel
    cadence. An active row keeps the loud ERROR: a loopback / misregistered
    gateway_url that makes the gateway dial itself and answer under its own
    name is exactly the 2026-07-18 incident class and must not go quiet.
    """
    first_of_episode = name not in _identity_mismatch_active
    _identity_mismatch_active.add(name)
    if not first_of_episode:
        return
    if stopped:
        _log.info(
            "identity mismatch on stopped machine %r at %s: the ops server there "
            "self-reported %r — the row's stale URL answers for another host; "
            "reported once until the identity echoes correctly again",
            name,
            gateway_url,
            responder,
        )
    else:
        _log.error(
            "identity mismatch: probing machine %r at %s, but the ops server self-reported %r",
            name,
            gateway_url,
            responder,
        )


def note_identity_match(name: str) -> None:
    """End `name`'s mismatch episode — its identity echoed correctly again."""
    _identity_mismatch_active.discard(name)
