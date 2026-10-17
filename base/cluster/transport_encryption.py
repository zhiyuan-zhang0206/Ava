"""Deployment precondition for secret-bearing off-box listeners."""

TRANSPORT_ENCRYPTION_MODES = frozenset({"tls", "mtls", "overlay"})
_LOOPBACK_BIND_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class TransportEncryptionUndeclared(RuntimeError):  # noqa: N818 — public exception name is specified
    """Raised when an off-box authenticated listener lacks an encryption declaration."""

    def __init__(self) -> None:
        super().__init__(
            "An authenticated cluster serving off-box must declare "
            "MEIVA_TRANSPORT_ENCRYPTION as one of: tls, mtls, overlay. See "
            "docs/conventions/runbook.md#transport-encryption."
        )


def verify_transport_encryption_declaration(
    bind_host: str,
    declaration: str,
    *,
    authenticated: bool,
) -> None:
    """Verify an explicit projection without resolving ordinary Settings."""
    if not _requires_transport_encryption_declaration(bind_host, authenticated=authenticated):
        return

    if declaration not in TRANSPORT_ENCRYPTION_MODES:
        raise TransportEncryptionUndeclared


def _requires_transport_encryption_declaration(bind_host: str, *, authenticated: bool) -> bool:
    normalized_host = bind_host.strip().lower().removeprefix("[").removesuffix("]")
    return authenticated and normalized_host not in _LOOPBACK_BIND_HOSTS
