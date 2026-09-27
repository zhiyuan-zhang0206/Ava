"""Existing release selector CAS, authorized by the current pending deployment.

Hashing and file reads happen before the short authority transaction. The final
PG-to-filesystem boundary is not atomic; every following effect revalidates the
same operation and exact selector. A crash retains the prior bytes in the
existing updater journal; restoration requires a fresh authorized reverse plan.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from services.agent_ops.bootstrap import PreparedObservation
from shared.managed_writer_activation import SelectorReadback, require_pending_selector_change
from shared.managed_writer_publication import PublishedUnit
from shared.platform import file_lock
from shared.runtime_publication_input import PublicationSelector, _receipt_expected
from shared.runtime_release import ReleaseRejectedError, VerifiedRelease, verify_release
from shared.verified_file import regular_bytes


@contextmanager
def pending_transaction(
    conn: psycopg.Connection, context: PreparedObservation
) -> Generator[None, None, None]:
    """Bound each lock/query by the same absolute operation challenge."""
    with conn.transaction():
        remaining = int((context.challenge.valid_until - datetime.now(UTC)).total_seconds() * 1000)
        if remaining <= 0:
            raise ReleaseRejectedError("pending transaction challenge expired")
        conn.execute("SELECT set_config('statement_timeout', %s, true)", (str(remaining),))
        yield


def selector_bytes(unit: PublishedUnit) -> bytes:
    selector = PublicationSelector(
        version=2,
        artifact_digest=unit.artifact_digest,
        manifest_digest=unit.manifest_digest,
        prepared_receipt_digest=unit.prepared_receipt_digest,
    )
    return (
        json.dumps(selector.model_dump(), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def read_selector(home: Path) -> bytes | None:
    store = home / "releases"
    if home.resolve(strict=True) != home or store.resolve(strict=True) != store:
        raise ReleaseRejectedError("selector home/store is not canonical")
    try:
        return regular_bytes(store / "current-release")
    except FileNotFoundError:
        return None


def verify_unit_image(unit: PublishedUnit, schema_digest: str) -> VerifiedRelease:
    home = Path(unit.home)
    if (home / "machine_name").read_text().strip() != unit.machine:
        raise ReleaseRejectedError("candidate unit machine changed")
    image = verify_release(
        home / "releases",
        unit.artifact_digest,
        manifest_digest=unit.manifest_digest,
        platform_tag=platform.platform(),
        schema_digest=schema_digest,
    )
    receipt = regular_bytes(home / "run" / f"release-inventory-{unit.prepared_receipt_digest}.json")
    if hashlib.sha256(receipt).hexdigest() != unit.prepared_receipt_digest:
        raise ReleaseRejectedError("complete prepared receipt changed")
    expected = _receipt_expected(receipt)
    if (
        expected.machine,
        expected.home,
        expected.artifact_digest,
        expected.manifest_digest,
        expected.unit().inventory_digest,
    ) != (
        unit.machine,
        unit.home,
        unit.artifact_digest,
        unit.manifest_digest,
        unit.inventory_digest,
    ):
        raise ReleaseRejectedError("prepared receipt and candidate unit differ")
    return image


def select_pending_release(
    conn: psycopg.Connection,
    context: PreparedObservation,
    unit: PublishedUnit,
    previous: bytes | None,
) -> SelectorReadback:
    """Commit exactly the prepared pointer after real migration/barrier authority.

    This is also the reverse-CAS mechanism: rollback has a NEW operation and a
    newly prepared/adopted reverse plan. Old operation receipts cannot restore.
    """
    image = verify_unit_image(unit, context.schema_digest)
    if not Path(__file__).resolve().is_relative_to(image.root / "venv"):
        raise ReleaseRejectedError("selector writer is not the loaded candidate runtime")
    body = selector_bytes(unit)
    before_digest = hashlib.sha256(previous).hexdigest() if previous is not None else None
    after_digest = hashlib.sha256(body).hexdigest()
    store = image.root.parent
    lock = store / "activation.lock"
    if lock.is_symlink():
        raise ReleaseRejectedError("selector lock is a symlink")
    with file_lock(lock, timeout_s=5):
        observed = read_selector(Path(unit.home))
        if observed not in (previous, body):
            raise ReleaseRejectedError("selector predecessor changed")
        with pending_transaction(conn, context):
            plan = require_pending_selector_change(
                conn, context.operation, context.challenge.challenge, unit
            )
            if (
                plan.previous_selector_digest != before_digest
                or plan.selector_digest != after_digest
            ):
                raise ReleaseRejectedError("selector bytes differ from the pending plan")
        if datetime.now(UTC) >= context.challenge.valid_until:
            raise ReleaseRejectedError("selector challenge expired before filesystem effect")
        if observed != body:
            fd, filename = tempfile.mkstemp(prefix=".current-release-", dir=store)
            temporary = Path(filename)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(store / "current-release")
                directory = os.open(store, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                temporary.unlink(missing_ok=True)
        if read_selector(Path(unit.home)) != body:
            raise ReleaseRejectedError("selector readback differs from the committed bytes")
    with pending_transaction(conn, context):
        require_pending_selector_change(conn, context.operation, context.challenge.challenge, unit)
    return SelectorReadback(
        unit=unit,
        challenge=context.challenge.challenge,
        previous_digest=before_digest,
        current_digest=after_digest,
        observed_at=datetime.now(UTC),
        valid_until=context.challenge.valid_until,
    )
