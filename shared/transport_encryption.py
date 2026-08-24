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
    normalized_host = bind_host.strip().lower().removeprefix("[").removesuffix("]")
    if not cluster_secret or normalized_host in _LOOPBACK_BIND_HOSTS:
        return

    from shared.config import settings

    if settings.data_plane.transport_encryption not in TRANSPORT_ENCRYPTION_MODES:
        raise TransportEncryptionUndeclared
