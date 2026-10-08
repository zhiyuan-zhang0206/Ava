"""Durable source acceptance and retained single-inbound delivery outcome."""

from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.agents import AgentNotFound
from base.agents.messages.chat_delivery import insert_chat_inbound_in_transaction
from base.agents.messages.inbound_provenance import InboundProvenance
from base.agents.upload_delivery import storage
from base.agents.upload_delivery.models import (
    NAMESPACE,
    Acceptance,
    CopyProof,
    DeliveryStatus,
    Manifest,
    Object,
    ReceiveRequest,
    UploadDeliveryConflictError,
    current_unit,
    make_manifest,
    request_hash,
)
from base.agents.uploads import MAX_AGENT_UPLOAD_BYTES, MAX_AGENT_UPLOAD_FILES, agent_upload_dir
from base.cluster.authority.unit import UnitIdentity
from base.db.transaction import write_transaction


def target_unit(conn: Connection[Any], agent_id: int) -> UnitIdentity:
    if (
        conn.execute("SELECT id FROM agents WHERE id=%s FOR NO KEY UPDATE", (agent_id,)).fetchone()
        is None
    ):
        raise AgentNotFound(agent_id)
    agent = conn.execute(
        "SELECT machine FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,)
    ).fetchone()
    if agent is None:
        raise AgentNotFound(agent_id)
    rows = conn.execute(
        "SELECT u.machine_name, u.home FROM machine_units u JOIN machines m "
        "ON m.name=u.machine_name WHERE m.name=%s AND u.serve_agent_runner "
        "AND u.url=m.gateway_url FOR SHARE OF u,m",
        (agent[0],),
    ).fetchall()
    if len(rows) != 1:
        raise UploadDeliveryConflictError("target has no unique registered serving runner unit")
    return UnitIdentity(machine=rows[0][0], home=rows[0][1])


def _lookup(conn: Connection[Any], key: str, fingerprint: str) -> tuple[Any, ...] | None:
    row = conn.execute(
        "SELECT request_hash, manifest, source_unit, target_unit, acceptance, storage_directory "
        "FROM upload_delivery_batches WHERE operation_key=%s",
        (key,),
    ).fetchone()
    if row is not None and row[0] != fingerprint:
        raise UploadDeliveryConflictError(
            "idempotency key already identifies another upload manifest"
        )
    return row


def _claim(
    pool: ConnectionPool,
    key: str,
    agent_id: int,
    names: list[str],
    batch: list[tuple[str, bytes, str]],
    provenance: InboundProvenance,
) -> tuple[Any, ...]:
    fingerprint = request_hash(names, batch)
    source = current_unit()
    directory = agent_upload_dir(agent_id, create=False).resolve()
    with write_transaction(pool) as conn:
        storage.lock_agent(conn, agent_id)
        found = _lookup(conn, key, fingerprint)
        if found is not None:
            return found
        manifest = make_manifest(agent_id, names, batch)
        target = target_unit(conn, agent_id)
        storage.check_quota(
            conn,
            agent_id,
            directory,
            source.machine,
            sum(item.size for item in manifest.objects),
            len(manifest.objects),
            MAX_AGENT_UPLOAD_BYTES,
            MAX_AGENT_UPLOAD_FILES,
        )
        conn.execute(
            "INSERT INTO upload_delivery_batches "
            "(batch_id, operation_key, agent_id, request_hash, manifest, source_unit, target_unit, "
            "storage_machine, storage_directory, provenance) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                manifest.batch_id,
                key,
                agent_id,
                fingerprint,
                Jsonb(manifest.model_dump()),
                Jsonb(source.model_dump()),
                Jsonb(target.model_dump()),
                source.machine,
                str(directory),
                Jsonb(
                    {
                        "source_verified_by": provenance.source_verified_by,
                        "source_transport": provenance.source_transport,
                    }
                ),
            ),
        )
        return (
            fingerprint,
            manifest.model_dump(),
            source.model_dump(),
            target.model_dump(),
            None,
            str(directory),
        )


def accept(
    pool: ConnectionPool,
    key: str,
    agent_id: int,
    names: list[str],
    batch: list[tuple[str, bytes, str]],
    provenance: InboundProvenance,
) -> Acceptance:
    claimed = _claim(pool, key, agent_id, names, batch, provenance)
    if claimed[4] is not None:
        return Acceptance.model_validate(claimed[4])
    if UnitIdentity.model_validate(claimed[2]) != current_unit():
        raise UploadDeliveryConflictError("receiving source belongs to another native unit")
    manifest = Manifest.model_validate(claimed[1])
    directory = agent_upload_dir(agent_id, create=False).resolve()
    if str(directory) != claimed[5]:
        raise UploadDeliveryConflictError("receiving source binds another native physical root")
    # No DB connection is held across filesystem publication. Fixed namespace and
    # create-only primitive permit overlapping live writers after DB disconnection.
    storage.publish(directory, manifest, [item[1] for item in batch])
    acceptance = Acceptance(
        batch_id=manifest.batch_id,
        agent_id=agent_id,
        files=manifest.objects,
        status_url=f"/api/keyed/v1/agents/{agent_id}/uploads/{manifest.batch_id}",
    )
    with write_transaction(pool) as conn:
        storage.lock_agent(conn, agent_id)
        found = _lookup(conn, key, claimed[0])
        assert found is not None  # noqa: S101 -- retained receiving identity
        if found[4] is not None:
            return Acceptance.model_validate(found[4])
        storage.verify_all(directory, manifest)
        conn.execute(
            "UPDATE upload_delivery_batches SET acceptance=%s, ready_at=now(), state='pending' "
            "WHERE operation_key=%s",
            (Jsonb(acceptance.model_dump()), key),
        )
    return acceptance


def status(pool: ConnectionPool, agent_id: int, batch_id: str) -> DeliveryStatus:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT acceptance, state, reason, inbound_id, source_unit, target_unit, attempts, next_attempt_at FROM upload_delivery_batches "
            "WHERE agent_id=%s AND batch_id=%s",
            (agent_id, batch_id),
        ).fetchone()
    if row is None:
        raise AgentNotFound(agent_id)
    return DeliveryStatus(
        acceptance=None if row[0] is None else Acceptance.model_validate(row[0]),
        state=row[1],
        reason=row[2],
        inbound_id=row[3],
        source=UnitIdentity.model_validate(row[4]),
        target=UnitIdentity.model_validate(row[5]),
        attempts=row[6],
        next_attempt_at=row[7] if row[1] == "pending" else None,
    )


def served_object(
    pool: ConnectionPool, agent_id: int, batch_id: str, ordinal: int
) -> tuple[Path, Object]:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT manifest, source_unit, storage_directory FROM upload_delivery_batches "
            "WHERE agent_id=%s AND batch_id=%s AND ready_at IS NOT NULL",
            (agent_id, batch_id),
        ).fetchone()
    if row is None or UnitIdentity.model_validate(row[1]) != current_unit():
        raise UploadDeliveryConflictError("source object is not ready on this native unit")
    manifest = Manifest.model_validate(row[0])
    if not 0 <= ordinal < len(manifest.objects):
        raise UploadDeliveryConflictError("object ordinal is absent from the fixed manifest")
    item = manifest.objects[ordinal]
    path = storage.object_path(Path(row[2]), manifest, item)
    storage.verify(path, item)
    return path, item


def due(pool: ConnectionPool, limit: int = 8) -> list[ReceiveRequest]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT manifest, source_unit, target_unit FROM upload_delivery_batches "
            "WHERE state='pending' AND next_attempt_at<=now() AND source_unit=%s "
            "ORDER BY next_attempt_at, batch_id LIMIT %s",
            (Jsonb(current_unit().model_dump()), limit),
        ).fetchall()
    return [
        ReceiveRequest(
            version=1,
            manifest=Manifest.model_validate(row[0]),
            source=UnitIdentity.model_validate(row[1]),
            target=UnitIdentity.model_validate(row[2]),
        )
        for row in rows
    ]


def validate_pending(pool: ConnectionPool, request: ReceiveRequest) -> bool:
    """Fence known placement changes before copy; final acceptance rechecks under lock."""
    with write_transaction(pool) as conn:
        row = conn.execute(
            "SELECT state FROM upload_delivery_batches WHERE batch_id=%s FOR UPDATE",
            (request.manifest.batch_id,),
        ).fetchone()
        if row is None or row[0] != "pending":
            return False
        if target_unit(conn, request.manifest.agent_id) != request.target:
            raise UploadDeliveryConflictError("agent placement changed after upload acceptance")
        return True


def record_failure(pool: ConnectionPool, batch_id: str, reason: str, *, hold: bool) -> None:
    with write_transaction(pool) as conn:
        conn.execute(
            "UPDATE upload_delivery_batches SET state=%s, reason=%s, attempts=attempts+1, "
            "next_attempt_at=now()+interval '30 seconds' * least(attempts+1, 20) "
            "WHERE batch_id=%s AND state='pending'",
            ("hold" if hold else "pending", reason, batch_id),
        )


def _proof_paths(proof: CopyProof, manifest: Manifest) -> list[str]:
    native_directory = (
        PurePosixPath(proof.directory)
        if PurePosixPath(proof.directory).is_absolute()
        else PureWindowsPath(proof.directory)
    )
    if (
        native_directory.name != manifest.batch_id
        or native_directory.parent.name != NAMESPACE
        or native_directory.parent.parent.name != f"AvaAgent-{manifest.agent_id}"
    ):
        raise UploadDeliveryConflictError(
            "copy proof directory differs from its fixed agent batch namespace"
        )
    return [str(native_directory / item.name) for item in manifest.objects]


def complete(pool: ConnectionPool, request: ReceiveRequest, proof: CopyProof) -> int:
    if (
        proof.version != 1
        or proof.target != request.target
        or proof.manifest_hash != request.manifest.fingerprint()
    ):
        raise UploadDeliveryConflictError("receiver proof differs from the frozen target manifest")
    manifest = request.manifest
    with write_transaction(pool) as conn:
        row = conn.execute(
            "SELECT inbound_id, target_unit, manifest, provenance, state, source_unit "
            "FROM upload_delivery_batches "
            "WHERE batch_id=%s AND ready_at IS NOT NULL FOR UPDATE",
            (manifest.batch_id,),
        ).fetchone()
        if row is None:
            raise UploadDeliveryConflictError("source acceptance is absent")
        if (
            UnitIdentity.model_validate(row[1]) != request.target
            or Manifest.model_validate(row[2]) != manifest
            or UnitIdentity.model_validate(row[5]) != request.source
        ):
            raise UploadDeliveryConflictError("stored intent differs from receiver proof")
        if row[0] is not None:
            return row[0]
        if row[4] != "pending":
            raise UploadDeliveryConflictError("held delivery cannot accept a late copy proof")
        if target_unit(conn, manifest.agent_id) != request.target:
            raise UploadDeliveryConflictError("agent placement changed after upload acceptance")
        paths = _proof_paths(proof, manifest)
        content = (
            "Uploaded files on "
            + request.target.machine
            + ":\n"
            + "\n".join(
                f"- {item.filename}: {path}"
                for item, path in zip(manifest.objects, paths, strict=True)
            )
        )
        receipt, _ = insert_chat_inbound_in_transaction(
            conn,
            agent_id=manifest.agent_id,
            content=content,
            source="user",
            payload=None,
            client_message_id=f"upload-delivery-v1:{manifest.batch_id}",
            provenance=InboundProvenance(**row[3]),
        )
        conn.execute(
            "UPDATE upload_delivery_batches SET state='accepted', reason=NULL, inbound_id=%s, "
            "copy_proof=%s, outcome=%s WHERE batch_id=%s",
            (
                receipt.inbound_id,
                Jsonb(proof.model_dump()),
                Jsonb({"inbound_id": receipt.inbound_id, "content": content}),
                manifest.batch_id,
            ),
        )
        return receipt.inbound_id


def pending_wakes(
    pool: ConnectionPool, after: str = "", limit: int = 32
) -> list[tuple[str, int, int]]:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT d.batch_id, d.agent_id, d.inbound_id FROM upload_delivery_batches d "
            "JOIN inbound_messages i ON i.id=d.inbound_id WHERE d.state='accepted' "
            "AND i.agent_id=d.agent_id AND i.status='pending' AND d.source_unit=%s "
            "ORDER BY (d.batch_id<=%s), d.batch_id LIMIT %s",
            (Jsonb(current_unit().model_dump()), after, limit),
        ).fetchall()
