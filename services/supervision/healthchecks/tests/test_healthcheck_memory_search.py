"""`services.supervision.healthchecks.memory_search` unit tests — probe seam + restart shape.

The healthcheck probes with a real POST /search (the store path the
gateway/indexer dial), not /healthz — a port-open probe stays green while
the service behind it is unusable. These tests pin that the probe wraps
the search answer into a DaemonProbe verdict (alive/down) and that the
restart path reports the probe's verdict rather than the spawn's — the
healthcheck runs the shared keepalive body (`run_keepalive`), which is
covered by `services/supervision/healthchecks/tests/test_healthcheck_gateway.py`-style tests of
`base.service_respawn`.
"""

from __future__ import annotations

import pytest

from base.daemon.health import DaemonProbe
from services.supervision.healthchecks import memory_search as hc


def test_probe_up_when_search_answers_with_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hc, "_post_search", lambda _uri: {"paths": []})  # pyright: ignore[reportUnknownArgumentType]
    result = hc._probe()
    assert result.alive is True
    assert result.terminal is False


def test_probe_down_when_search_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        hc,
        "_post_search",
        lambda _uri: DaemonProbe.down("connection refused"),  # pyright: ignore[reportUnknownArgumentType]
    )
    result = hc._probe()
    assert result.alive is False
    assert "connection refused" in result.detail


def test_probe_down_when_payload_lacks_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """A foreign process answering 200 on the port is not the search service."""
    monkeypatch.setattr(hc, "_post_search", lambda _uri: {"nope": 1})  # pyright: ignore[reportUnknownArgumentType]
    assert hc._probe().alive is False


def test_search_probe_uses_metadata_without_constructing_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    import httpx

    from services.derived.memory_indexer.embeddings import factory

    descriptor = factory.get_descriptor()
    constructor = Mock(side_effect=AssertionError("health probes must not construct a provider"))
    monkeypatch.setattr(factory, "get_provider", constructor)
    response = Mock()
    response.json.return_value = {"paths": []}
    post = Mock(return_value=response)
    monkeypatch.setattr(httpx, "post", post)
    assert hc._post_search("http://memory-search") == {"paths": []}
    assert post.call_args.kwargs["json"] == {"vector": [0.0] * descriptor.dim, "k": 1}
    response.raise_for_status.assert_called_once_with()
    constructor.assert_not_called()
