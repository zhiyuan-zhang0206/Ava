"""Transport-encryption declaration checks for off-box secret clusters."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from shared.config import settings
from shared.transport_encryption import (
    TransportEncryptionUndeclared,
    verify_transport_encryption,
)


def test_test_cluster_declares_transport_encryption() -> None:
    """The suite's secret-bearing e2e ops daemon binds off-box."""
    assert os.environ["AVA_TRANSPORT_ENCRYPTION"] == "overlay"
    assert (
        "AVA_TRANSPORT_ENCRYPTION=overlay"
        in (Path(os.environ["AVA_HOME"]) / ".env").read_text().splitlines()
    )


def test_no_secret_does_not_require_transport_encryption() -> None:
    verify_transport_encryption("", "0.0.0.0")  # noqa: S104 — test input, not a listener bind


@pytest.mark.parametrize("bind_host", ("127.0.0.1", "::1", "localhost"))
def test_loopback_bind_does_not_require_transport_encryption(bind_host: str) -> None:
    verify_transport_encryption("cluster-secret", bind_host)


def test_undeclared_transport_encryption_refuses_off_box_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.data_plane, "transport_encryption", "")

    with pytest.raises(TransportEncryptionUndeclared) as exc:
        verify_transport_encryption("cluster-secret", "0.0.0.0")  # noqa: S104 — test input

    message = str(exc.value)
    assert "AVA_TRANSPORT_ENCRYPTION" in message
    assert "tls, mtls, overlay" in message
    assert "conventions/runbook.md" in message


@pytest.mark.parametrize("mode", ("tls", "mtls", "overlay"))
def test_declared_transport_encryption_permits_off_box_bind(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setattr(settings.data_plane, "transport_encryption", mode)

    verify_transport_encryption("cluster-secret", "0.0.0.0")  # noqa: S104 — test input


def test_invalid_transport_encryption_refuses_off_box_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.data_plane, "transport_encryption", "wireguard")

    with pytest.raises(TransportEncryptionUndeclared):
        verify_transport_encryption("cluster-secret", "0.0.0.0")  # noqa: S104 — test input
