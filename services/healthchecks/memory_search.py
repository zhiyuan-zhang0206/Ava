"""Read-only health probes for memory search; the root supervisor owns recovery."""

from __future__ import annotations

from shared.config import settings
from shared.daemon_health import DaemonProbe

_TIMEOUT_S = 3.0


def _post_search(uri: str) -> dict[str, object] | DaemonProbe:
    """One POST /search with a zero vector — the body dict when it
    answered, or the not-alive verdict that trying produced (fail
    closed: any failure means "not alive", per the module docstring)."""
    import httpx

    from services.memory_indexer.embeddings.factory import get_provider

    try:
        resp = httpx.post(
            f"{uri}/search",
            json={"vector": [0.0] * get_provider().dim, "k": 1},
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return DaemonProbe.down(f"POST /search failed ({type(exc).__name__}: {exc})")


def _probe() -> DaemonProbe:
    """Alive when the service answers a real search with a paths payload."""
    payload = _post_search(settings.services.memory_search_uri)
    if isinstance(payload, DaemonProbe):
        return payload
    if "paths" in payload:
        return DaemonProbe.up("POST /search answered with a paths payload")
    return DaemonProbe.down("POST /search answered without a paths payload")
