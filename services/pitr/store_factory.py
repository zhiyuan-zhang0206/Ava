"""One env var selects the PITR object-store backend.

The same discipline as ``services.memory_indexer.backends.factory``:
``get_backend()`` is the single dispatch path, keyed by
``settings.physical_backup.pitr_store_backend``
(``AVA_PITR_STORE_BACKEND``, default ``gcs``). An unrecognized value
fails fast — a typo must never silently fall back to the previous
backend while the operator believes the switch happened.

Each backend exposes the four PITR store roles plus the protected-manifest
publisher as constructors; daemons ask the selected backend for the role
they need. The four role contracts stay separate (an adapter may serve
several roles internally, but no caller gets a merged surface). Adding a
backend = one module here + its adapter modules.

Imports are lazy by design: the restricted restore worker imports this
module to construct its reader, and its exec boundary must stay free of
``shared.config`` and the publish-store adapter, so no adapter module and
no settings access happen at import time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from services.pitr.base_object_store import RestartableStreamingObjectStore
from services.pitr.object_store import ObjectStore
from services.pitr.restore_object_store import GenerationPinnedObjectReader
from services.pitr.restore_proof import ProtectedManifestPublisher
from services.pitr.retention_inventory import RetentionInventoryReader


class PitrStoreBackend(Protocol):
    """One object-store backend; each method constructs one role adapter."""

    name: str

    def object_store(
        self, *, project: str, bucket: str, credentials_file: Path, timeout_seconds: int = 30
    ) -> ObjectStore: ...

    def restartable_streaming_object_store(
        self, *, project: str, bucket: str, credentials_file: str, timeout_seconds: int = 300
    ) -> RestartableStreamingObjectStore: ...

    def generation_pinned_object_reader(
        self, *, project: str, bucket: str, credentials_file: Path, timeout_seconds: int = 300
    ) -> GenerationPinnedObjectReader: ...

    def retention_inventory_reader(
        self, *, project: str, bucket: str, prefix: str, credentials_file: Path
    ) -> RetentionInventoryReader: ...

    def protected_manifest_publisher(
        self, *, project: str, bucket: str, credentials_file: Path, timeout_seconds: int = 60
    ) -> ProtectedManifestPublisher: ...


class GcsPitrStoreBackend:
    """The baseline backend; every role delegates to the existing GCS adapter."""

    name = "gcs"

    @staticmethod
    def object_store(
        *, project: str, bucket: str, credentials_file: Path, timeout_seconds: int = 30
    ) -> ObjectStore:
        from services.pitr.gcs_store import GCSObjectStore

        return GCSObjectStore(
            project=project,
            bucket=bucket,
            credentials_file=credentials_file,
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def restartable_streaming_object_store(
        *, project: str, bucket: str, credentials_file: str, timeout_seconds: int = 300
    ) -> RestartableStreamingObjectStore:
        from services.pitr.base_object_store import GCSRestartableStreamingObjectStore

        return GCSRestartableStreamingObjectStore(
            project=project,
            bucket=bucket,
            credentials_file=credentials_file,
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def generation_pinned_object_reader(
        *, project: str, bucket: str, credentials_file: Path, timeout_seconds: int = 300
    ) -> GenerationPinnedObjectReader:
        from services.pitr.restore_object_store import GCSGenerationPinnedObjectReader

        return GCSGenerationPinnedObjectReader(
            project=project,
            bucket=bucket,
            credentials_file=credentials_file,
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def retention_inventory_reader(
        *, project: str, bucket: str, prefix: str, credentials_file: Path
    ) -> RetentionInventoryReader:
        from services.pitr.retention_inventory import GCSRetentionInventoryReader

        return GCSRetentionInventoryReader(
            project=project,
            bucket=bucket,
            prefix=prefix,
            credentials_file=credentials_file,
        )

    @staticmethod
    def protected_manifest_publisher(
        *, project: str, bucket: str, credentials_file: Path, timeout_seconds: int = 60
    ) -> ProtectedManifestPublisher:
        from services.pitr.restore_publish_store import GCSProtectedManifestPublisher

        return GCSProtectedManifestPublisher(
            project=project,
            bucket=bucket,
            credentials_file=credentials_file,
            timeout_seconds=timeout_seconds,
        )


_BACKENDS: dict[str, PitrStoreBackend] = {GcsPitrStoreBackend.name: GcsPitrStoreBackend()}


def get_backend_named(name: str) -> PitrStoreBackend:
    """Construct a backend by name — the one dispatch path; unknown names
    fail fast (an unrecognized value must not silently fall back to GCS:
    a typo would otherwise keep the old storage while the operator
    believes the switch happened)."""
    try:
        return _BACKENDS[name]
    except KeyError:
        known = ", ".join(sorted(_BACKENDS))
        raise ValueError(f"unknown PITR store backend {name!r} (known: {known})") from None


def get_backend() -> PitrStoreBackend:
    """Construct the configured backend
    (``settings.physical_backup.pitr_store_backend``, env
    ``AVA_PITR_STORE_BACKEND``)."""
    from shared.config import settings

    return get_backend_named(settings.physical_backup.pitr_store_backend)
