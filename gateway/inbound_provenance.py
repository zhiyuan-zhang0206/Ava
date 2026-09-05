"""Translate verified gateway request state into inbound audit facts."""

from __future__ import annotations

from starlette.requests import Request

from shared.inbound_provenance import InboundProvenance


def request_inbound_provenance(
    request: Request,
    *,
    transport: str = "http",
) -> InboundProvenance:
    """Read only auth-middleware-owned state; never inspect caller JSON."""
    verified_by = getattr(request.state, "source_verified_by", None)
    if verified_by is not None and not isinstance(verified_by, str):
        raise TypeError("request source_verified_by must be a string or None")
    return InboundProvenance(
        source_verified_by=verified_by,
        source_transport=transport,
    )
