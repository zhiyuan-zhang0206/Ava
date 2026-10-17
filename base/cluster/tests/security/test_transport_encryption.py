"""Transport-encryption declaration checks for off-box authenticated listeners."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from base.cluster.transport_encryption import (
    TransportEncryptionUndeclared,
    verify_transport_encryption_declaration,
)


def test_test_cluster_declares_transport_encryption() -> None:
    """The suite's secret-bearing e2e ops daemon binds off-box."""
    assert os.environ["MEIVA_TRANSPORT_ENCRYPTION"] == "overlay"
    assert (
        "MEIVA_TRANSPORT_ENCRYPTION=overlay"
        in (Path(os.environ["MEIVA_HOME"]) / ".env").read_text().splitlines()
    )


def test_open_listener_does_not_require_transport_encryption() -> None:
    verify_transport_encryption_declaration("0.0.0.0", "", authenticated=False)  # noqa: S104 — test input, not a listener bind


@pytest.mark.parametrize("bind_host", ("127.0.0.1", "::1", "localhost"))
def test_loopback_bind_does_not_require_transport_encryption(bind_host: str) -> None:
    verify_transport_encryption_declaration(bind_host, "", authenticated=True)


def test_undeclared_transport_encryption_refuses_off_box_bind() -> None:

    with pytest.raises(TransportEncryptionUndeclared) as exc:
        verify_transport_encryption_declaration("0.0.0.0", "", authenticated=True)  # noqa: S104 — test input

    message = str(exc.value)
    assert "MEIVA_TRANSPORT_ENCRYPTION" in message
    assert "tls, mtls, overlay" in message
    assert "docs/conventions/runbook.md" in message


@pytest.mark.parametrize("mode", ("tls", "mtls", "overlay"))
def test_declared_transport_encryption_permits_off_box_bind(mode: str) -> None:

    verify_transport_encryption_declaration("0.0.0.0", mode, authenticated=True)  # noqa: S104 — test input


def test_invalid_transport_encryption_refuses_off_box_bind() -> None:

    with pytest.raises(TransportEncryptionUndeclared):
        verify_transport_encryption_declaration("0.0.0.0", "wireguard", authenticated=True)  # noqa: S104 — test input
