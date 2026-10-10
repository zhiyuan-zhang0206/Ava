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

import os
import time
from collections.abc import Iterator
from unittest.mock import Mock, patch

import pytest

from base.daemon.health import DaemonProbe
from services.supervision.healthchecks import memory_search as hc


@pytest.fixture
def owned_config_environment() -> Iterator[None]:
    """Restore environment delivery and the process timezone after a real ConfigBoot."""
    try:
        with patch.dict(os.environ):
            yield
    finally:
        tzset = getattr(time, "tzset", None)
        if tzset is not None:
            tzset()


@pytest.mark.usefixtures("owned_config_environment")
def test_probe_up_when_search_answers_with_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hc, "_post_search", Mock(return_value={"paths": []}))
    result = hc._probe()
    assert result.alive is True
    assert result.terminal is False


@pytest.mark.usefixtures("owned_config_environment")
def test_probe_down_when_search_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        hc,
        "_post_search",
        Mock(return_value=DaemonProbe.down("connection refused")),
    )
    result = hc._probe()
    assert result.alive is False
    assert "connection refused" in result.detail


@pytest.mark.usefixtures("owned_config_environment")
def test_probe_down_when_payload_lacks_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """A foreign process answering 200 on the port is not the search service."""
    monkeypatch.setattr(hc, "_post_search", Mock(return_value={"nope": 1}))
    assert hc._probe().alive is False


def test_search_probe_uses_metadata_without_constructing_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from services.derived.memory_indexer.embeddings import factory

    descriptor = factory.get_descriptor("gemini")
    constructor = Mock(side_effect=AssertionError("health probes must not construct a provider"))
    monkeypatch.setattr(factory, "get_provider", constructor)
    response = Mock()
    response.json.return_value = {"paths": []}
    post = Mock(return_value=response)
    monkeypatch.setattr(httpx, "post", post)
    assert hc._post_search("http://memory-search", embedding_name_reader=lambda: "gemini") == {
        "paths": []
    }
    assert post.call_args.kwargs["json"] == {"vector": [0.0] * descriptor.dim, "k": 1}
    response.raise_for_status.assert_called_once_with()
    constructor.assert_not_called()


def test_unknown_provider_fails_before_search_request(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    post = Mock()
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(ValueError, match="unknown embedding provider"):
        hc._post_search("http://memory-search", embedding_name_reader=lambda: "unknown-provider")
    post.assert_not_called()
