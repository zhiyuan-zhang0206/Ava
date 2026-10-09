"""A runner's bootstrap fetch against the live gateway endpoint."""

import os
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from base import config
from base.config.domains.storage.data_plane import self_machine_host
from base.host.env import bootstrap
from base.host.net.predicates import is_loopback_host
from base.host.net.url_secret import url_with_host
from gateway.app import app


def test_fetch_bootstrap_config_against_live_endpoint(
    db_conn,
    monkeypatch: pytest.MonkeyPatch,
    served_gateway_home: Any,
) -> None:
    # An authenticated gateway: the runner presents its delivered machine API
    # token (the active generation's runner token), never the human secret.
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", "live-secret")
    monkeypatch.setitem(os.environ, "AVA_API_TOKEN", served_gateway_home.api.runner)
    monkeypatch.delitem(os.environ, "AVA_CLUSTER_SECRET")

    # Route base.host.env.bootstrap's dial_get (base.host.net.http_dial.get) through the
    # in-process ASGI app.
    def fake_get(url, **kw):
        with TestClient(app) as c:
            return c.get(url.replace("http://cp", ""), **kw)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    monkeypatch.setattr(bootstrap, "dial_get", fake_get)  # pyright: ignore[reportUnknownArgumentType]
    values = bootstrap.fetch_bootstrap_config("http://cp")
    # Bootstrap serves the credential-free endpoint even though this gateway
    # keeps an active write generation: its runner login never travels here. A
    # multi-host gateway also rewrites its loopback host to the reachable
    # address for remote runners.
    runner = served_gateway_home.roles.runner
    expected = str(config.settings.data_plane.db_url)
    reachable = self_machine_host()
    if not is_loopback_host(reachable):
        expected = url_with_host(expected, reachable)
    actual_parts = urlsplit(values["AVA_DB_URL"])
    expected_parts = urlsplit(expected)
    # libpq dial hints such as hostaddr are implementation-specific query
    # parameters. The endpoint's connection identity must still match.
    assert (
        actual_parts.scheme,
        actual_parts.username,
        actual_parts.password,
        actual_parts.hostname,
        actual_parts.port,
        actual_parts.path.lstrip("/"),
    ) == (
        expected_parts.scheme,
        expected_parts.username,
        None,
        expected_parts.hostname,
        expected_parts.port,
        expected_parts.path.lstrip("/"),
    )
    assert runner.password not in "".join(values.values())
    # Nor does it serve the human secret: a remote unit never holds it.
    assert "AVA_CLUSTER_SECRET" not in values
    assert "test-cluster-secret" not in "".join(values.values())
