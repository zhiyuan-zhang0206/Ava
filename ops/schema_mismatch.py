"""Read-only diagnosis of the running image's required and applied migration sets."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg

import shared.db
from shared.api_contracts.status import SchemaMismatchKind, SchemaMismatchStatus
from shared.machine import machine_name
from shared.migration_errors import MigrationLayoutError
from shared.migration_layout import required_migration_set
from shared.migrations import applied_migration_names

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Mismatch:
    kind: SchemaMismatchKind
    detail: str


def classify(applied: set[str], required: set[str]) -> Mismatch | None:
    """Compare the current database with this running image, without a Git pin."""
    extra = applied - required
    missing = required - applied
    if extra and missing:
        return Mismatch(
            "divergent",
            f"DB has {len(extra)} migration(s) this image lacks and lacks "
            f"{len(missing)} required by this image",
        )
    if extra:
        return Mismatch(
            "schema-ahead-of-code", f"DB has {len(extra)} migration(s) this image lacks"
        )
    if missing:
        return Mismatch(
            "schema-behind-code", f"DB lacks {len(missing)} migration(s) required by this image"
        )
    return None


def detect(*, conn: psycopg.Connection | None = None) -> Mismatch | None:
    """Read applied names once; required names come from the running image's SQL."""
    try:
        if conn is None:
            with shared.db.connect(autocommit=True) as borrowed:
                applied = applied_migration_names(borrowed)
        else:
            applied = applied_migration_names(conn)
    except MigrationLayoutError as exc:
        return Mismatch("invalid-migration-layout", f"DB migration catalog is invalid: {exc}")
    except psycopg.Error as exc:
        _log.warning("[schema-mismatch] DB facts unavailable: %r", exc)
        return Mismatch("unavailable", f"applied migration set unavailable ({type(exc).__name__})")
    try:
        required = required_migration_set()
    except MigrationLayoutError as exc:
        return Mismatch("invalid-migration-layout", f"image migration layout is invalid: {exc}")
    return classify(applied, required)


def status(*, conn: psycopg.Connection | None = None) -> SchemaMismatchStatus | None:
    """Current diagnosis; return None only after both migration sets were read and match."""
    mismatch = detect() if conn is None else detect(conn=conn)
    if mismatch is None:
        return None
    return SchemaMismatchStatus(kind=mismatch.kind, machine=machine_name(), detail=mismatch.detail)
