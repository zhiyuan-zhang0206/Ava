"""Read-only health probes for memory search; the root supervisor owns recovery."""

from __future__ import annotations

from collections.abc import Callable

from base.config import ConfigBoot
from base.daemon.health import DaemonProbe

_TIMEOUT_S = 3.0


def _post_search(
    uri: str, *, embedding_name_reader: Callable[[], str]
) -> dict[str, object] | DaemonProbe:
    """One POST /search with a zero vector — the body dict when it
    answered, or the not-alive verdict that trying produced (fail
    closed: any failure means "not alive", per the module docstring)."""
    import httpx

    from services.derived.memory_indexer.embeddings.factory import get_descriptor

    dim = get_descriptor(embedding_name_reader()).dim
    try:
        resp = httpx.post(
            f"{uri}/search",
            json={"vector": [0.0] * dim, "k": 1},
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return DaemonProbe.down(f"POST /search failed ({type(exc).__name__}: {exc})")


def _probe() -> DaemonProbe:
    """Alive when the service answers a real search with a paths payload."""
    config = ConfigBoot()
    config.boot()
    payload = _post_search(
        config.view.services.memory_search_uri,
        embedding_name_reader=lambda: config.view.services.embedding_backend,
    )
    if isinstance(payload, DaemonProbe):
        return payload
    if "paths" in payload:
        return DaemonProbe.up("POST /search answered with a paths payload")
    return DaemonProbe.down("POST /search answered without a paths payload")
