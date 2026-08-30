"""One env var selects the PITR object-store backend.

The same discipline as ``services.memory_indexer.backends.factory``:
``get_store_group()`` is the single dispatch path, keyed by
``settings.physical_backup.pitr_store_backend``
(``AVA_PITR_STORE_BACKEND``, default ``gcs``). An unrecognized value
fails fast — a typo must never silently fall back to the previous
backend while the operator believes the switch happened.

A backend is a ``PitrStoreGroup``: five role factories (uploader /
restartable-streaming writer / viewer stat / generation-pinned reader /
retention inventory / protected-manifest publisher) bound to one
backend's credentials. The four role contracts stay separate (an
adapter may serve several roles internally, but no caller gets a
merged surface). Daemons take the settings-bound group; the restricted
restore worker builds the group explicitly from its input protocol,
which keeps its exec boundary free of ``shared.config``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from services.pitr.base_object_store import RestartableStreamingObjectStore
from services.pitr.object_store import ObjectStore
from services.pitr.restore_object_store import GenerationPinnedObjectReader
from services.pitr.restore_proof import ProtectedManifestPublisher
from services.pitr.retention_inventory import RetentionInventoryReader


@dataclass(frozen=True)
class PitrStoreGroup:
    """Five role factories bound to one backend and its credentials."""

    object_store: Callable[[], ObjectStore]
    restartable_streaming_object_store: Callable[[], RestartableStreamingObjectStore]
    viewer_object_store: Callable[[], ObjectStore]
    generation_pinned_object_reader: Callable[[], GenerationPinnedObjectReader]
    retention_inventory_reader: Callable[[], RetentionInventoryReader]
    protected_manifest_publisher: Callable[[], ProtectedManifestPublisher]


def gcs_pitr_store_group(
    *,
    project: str,
    bucket: str,
    prefix: str = "",
    uploader_credentials: str | Path | None = None,
    viewer_credentials: str | Path | None = None,
    timeout_seconds: int = 300,
) -> PitrStoreGroup:
    """The baseline backend: each role is the existing GCS adapter with its
    role-appropriate service account (uploader vs viewer-only). Adapter
    imports stay inside the role factories so constructing the group
    (e.g. in the restricted restore worker) imports nothing beyond the
    protocol modules."""

    def require_uploader() -> Path:
        if uploader_credentials is None:
            raise RuntimeError("validated PITR uploader credential is missing")
        return Path(uploader_credentials)

    def require_viewer() -> Path:
        if viewer_credentials is None:
            raise RuntimeError("validated PITR viewer credential is missing")
        return Path(viewer_credentials)

    def require_prefix() -> str:
        if not prefix:
            raise RuntimeError("validated PITR object prefix is missing")
        return prefix

    def object_store() -> ObjectStore:
        from services.pitr.gcs_store import GCSObjectStore

        return GCSObjectStore(
            project=project, bucket=bucket, credentials_file=require_uploader(), timeout_seconds=30
        )

    def restartable_streaming_object_store() -> RestartableStreamingObjectStore:
        from services.pitr.base_object_store import GCSRestartableStreamingObjectStore

        return GCSRestartableStreamingObjectStore(
            project=project,
            bucket=bucket,
            credentials_file=str(require_uploader()),
            timeout_seconds=timeout_seconds,
        )

    def viewer_object_store() -> ObjectStore:
        from services.pitr.gcs_store import GCSObjectStore

        return GCSObjectStore(
            project=project, bucket=bucket, credentials_file=require_viewer(), timeout_seconds=30
        )

    def generation_pinned_object_reader() -> GenerationPinnedObjectReader:
        from services.pitr.restore_object_store import GCSGenerationPinnedObjectReader

        return GCSGenerationPinnedObjectReader(
            project=project,
            bucket=bucket,
            credentials_file=require_viewer(),
            timeout_seconds=timeout_seconds,
        )

    def retention_inventory_reader() -> RetentionInventoryReader:
        from services.pitr.retention_inventory import GCSRetentionInventoryReader

        return GCSRetentionInventoryReader(
            project=project,
            bucket=bucket,
            prefix=require_prefix(),
            credentials_file=require_viewer(),
        )

    def protected_manifest_publisher() -> ProtectedManifestPublisher:
        from services.pitr.restore_publish_store import GCSProtectedManifestPublisher

        return GCSProtectedManifestPublisher(
            project=project, bucket=bucket, credentials_file=require_uploader()
        )

    return PitrStoreGroup(
        object_store=object_store,
        restartable_streaming_object_store=restartable_streaming_object_store,
        viewer_object_store=viewer_object_store,
        generation_pinned_object_reader=generation_pinned_object_reader,
        retention_inventory_reader=retention_inventory_reader,
        protected_manifest_publisher=protected_manifest_publisher,
    )


def baidu_pitr_store_group(
    *,
    app_root: str,
    prefix: str,
    credentials_file: str | Path,
    token_file: str | Path,
    timeout_seconds: float = 300.0,
) -> PitrStoreGroup:
    """The Baidu Netdisk backend: one token manager, one adapter class per
    role cluster (the object + streaming roles share one class)."""
    from services.pitr.baidu_inventory import BaiduRetentionInventoryReader
    from services.pitr.baidu_publish_store import BaiduProtectedManifestPublisher
    from services.pitr.baidu_restore_store import BaiduGenerationPinnedObjectReader
    from services.pitr.baidu_store import BaiduObjectStore
    from services.pitr.baidu_token import BaiduCredentials, BaiduTokenManager

    def token_manager() -> BaiduTokenManager:
        return BaiduTokenManager(BaiduCredentials(Path(credentials_file)), Path(token_file))

    def object_store() -> BaiduObjectStore:
        return BaiduObjectStore(
            app_root=app_root, token_manager=token_manager(), timeout_seconds=timeout_seconds
        )

    return PitrStoreGroup(
        object_store=object_store,
        restartable_streaming_object_store=object_store,
        viewer_object_store=object_store,
        generation_pinned_object_reader=lambda: BaiduGenerationPinnedObjectReader(
            app_root=app_root, token_manager=token_manager(), timeout_seconds=timeout_seconds
        ),
        retention_inventory_reader=lambda: BaiduRetentionInventoryReader(
            app_root=app_root,
            prefix=prefix,
            token_manager=token_manager(),
            timeout_seconds=timeout_seconds,
        ),
        protected_manifest_publisher=lambda: BaiduProtectedManifestPublisher(
            app_root=app_root, token_manager=token_manager(), timeout_seconds=timeout_seconds
        ),
    )


_GROUP_CONSTRUCTORS: dict[str, Callable[..., PitrStoreGroup]] = {
    "gcs": gcs_pitr_store_group,
    "baidu": baidu_pitr_store_group,
}


def get_group_constructor_named(name: str) -> Callable[..., PitrStoreGroup]:
    """Dispatch a backend's explicit group constructor by name — the path
    the restricted restore worker uses with its input-protocol args.
    Unknown names fail fast (a typo must not silently fall back to GCS:
    the operator would keep the old storage while believing the switch
    happened)."""
    try:
        return _GROUP_CONSTRUCTORS[name]
    except KeyError:
        known = ", ".join(sorted(_GROUP_CONSTRUCTORS))
        raise ValueError(f"unknown PITR store backend {name!r} (known: {known})") from None


def get_store_group() -> PitrStoreGroup:
    """The settings-bound group for the PITR daemons
    (``AVA_PITR_STORE_BACKEND``, default ``gcs``)."""
    from shared.config import settings

    config = settings.physical_backup
    name = config.pitr_store_backend
    if name not in _GROUP_CONSTRUCTORS:
        known = ", ".join(sorted(_GROUP_CONSTRUCTORS))
        raise ValueError(f"unknown PITR store backend {name!r} (known: {known})")
    if name == "gcs":
        return gcs_pitr_store_group(
            project=config.pitr_gcs_project,
            bucket=config.pitr_gcs_bucket,
            prefix=config.pitr_gcs_prefix,
            uploader_credentials=config.pitr_gcs_credentials_file,
            viewer_credentials=config.pitr_restore_gcs_credentials_file,
        )
    credentials_file = config.pitr_baidu_credentials_file
    token_file = config.pitr_baidu_token_file
    if credentials_file is None or token_file is None:
        raise RuntimeError("validated PITR Baidu credential/token path is missing")
    return baidu_pitr_store_group(
        app_root=config.pitr_baidu_app_root,
        prefix=config.pitr_gcs_prefix,
        credentials_file=credentials_file,
        token_file=token_file,
    )
