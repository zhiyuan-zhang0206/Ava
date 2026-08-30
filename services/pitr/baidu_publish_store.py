"""Immutable protected-manifest publisher for the Baidu backend."""

from __future__ import annotations

from services.pitr.baidu_store import BaiduObjectStore
from services.pitr.checksums import MD5, ObjectChecksum
from services.pitr.object_store import RemoteObjectAck
from services.pitr.token_manager import StoreTokenManager


class BaiduProtectedManifestPublisher:
    """Publish one small JSON manifest through the same content-addressed
    three-phase engine the objects use."""

    def __init__(
        self,
        *,
        app_root: str,
        token_manager: StoreTokenManager,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._store = BaiduObjectStore(
            app_root=app_root, token_manager=token_manager, timeout_seconds=timeout_seconds
        )

    def put_manifest_if_absent(
        self, *, payload: bytes, object_name: str, metadata: dict[str, str]
    ) -> RemoteObjectAck:
        import hashlib
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory(prefix="baidu-manifest-") as scratch:
            staged = Path(scratch) / "manifest.json"
            staged.write_bytes(payload)
            ack = self._store.put_wal_ciphertext_if_absent(staged, object_name, metadata)
        # The engine read back size + md5 already; the publisher additionally
        # confirms the stored body equals the payload (the GCS publisher
        # downloads and compares bytes).
        if ack.size != len(payload) or ack.checksum != ObjectChecksum(
            MD5,
            hashlib.md5(payload).hexdigest(),  # noqa: S324
        ):
            raise RuntimeError("immutable protected manifest remote ACK differs")
        return ack
