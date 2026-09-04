"""Deployment precondition for secret-bearing off-box listeners."""

TRANSPORT_ENCRYPTION_MODES = frozenset({"tls", "mtls", "overlay"})
_LOOPBACK_BIND_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class TransportEncryptionUndeclared(RuntimeError):  # noqa: N818 — public exception name is specified
    """Raised when an off-box secret listener lacks an encryption declaration."""

    def __init__(self) -> None:
        super().__init__(
            "A cluster with AVA_CLUSTER_SECRET serving off-box must declare "
            "AVA_TRANSPORT_ENCRYPTION as one of: tls, mtls, overlay. See "
            "conventions/runbook.md#transport-encryption."
        )


def verify_transport_encryption(cluster_secret: str, bind_host: str) -> None:
    """Refuse an off-box secret listener without an encryption declaration."""
    if not _requires_transport_encryption_declaration(cluster_secret, bind_host):
        return

    from shared.config import settings

    verify_transport_encryption_declaration(
        cluster_secret,
        bind_host,
        settings.data_plane.transport_encryption,
    )


def verify_transport_encryption_declaration(
    cluster_secret: str,
    bind_host: str,
    declaration: str,
) -> None:
    """Verify an explicit projection without resolving ordinary Settings."""
    if not _requires_transport_encryption_declaration(cluster_secret, bind_host):
        return

    if declaration not in TRANSPORT_ENCRYPTION_MODES:
        raise TransportEncryptionUndeclared


def _requires_transport_encryption_declaration(cluster_secret: str, bind_host: str) -> bool:
    normalized_host = bind_host.strip().lower().removeprefix("[").removesuffix("]")
    return bool(cluster_secret) and normalized_host not in _LOOPBACK_BIND_HOSTS
