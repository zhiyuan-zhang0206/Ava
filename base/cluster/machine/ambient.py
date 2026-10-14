"""The gateway access of a caller that has no configuration of its own yet.

TRANSITIONAL, and it leaves. A client that still reads the process-wide settings builds its
`GatewayAccess` here and marks the call `# ambient: <batch>`: the batch (B2, C, S, O) that gives that
component its own configuration root, from which it takes the access with `gateway_access_from_boot`.

Exit condition: `tests/test_ambient_callers.py` records every caller and fails on a new one or on a stale
entry, so the list only shrinks. The change that removes the last caller deletes this module and that
test in the same commit. New code never calls this; it receives a `GatewayAccess` from its root.
"""

from base.cluster.machine import GatewayAccess

__all__ = ["gateway_access_from_settings"]


def gateway_access_from_settings() -> GatewayAccess:
    """The gateway access the process-wide settings describe, read now."""
    from base.config import settings

    return GatewayAccess(
        gateway_url=settings.gateway.gateway_url,
        cluster_secret=settings.data_plane.cluster_secret,
        remote_plane=settings.data_plane.is_remote,
    )
