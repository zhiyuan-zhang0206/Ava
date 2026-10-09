"""Versioned immutable batch receiver; never falls back to overwriting upload_receive."""

import time

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.agents.upload_delivery import storage
from base.agents.upload_delivery.models import (
    NAMESPACE,
    CopyProof,
    ReceiveRequest,
    UploadDeliveryConflictError,
    current_unit,
)
from base.agents.upload_delivery.paths import (
    MAX_AGENT_UPLOAD_BYTES,
    MAX_AGENT_UPLOAD_FILES,
    agent_upload_dir,
)
from base.cluster.machine import gateway_api_base, gateway_auth_headers
from base.db.transaction import write_transaction
from base.host.net.http_dial import get as http_get


def receive(pool: ConnectionPool, request: ReceiveRequest) -> CopyProof:
    """Reserve native quota, pull immutable bytes, verify actual files, then acknowledge."""
    unit = current_unit()
    if request.target != unit:
        raise UploadDeliveryConflictError("copy targets a different native machine/home")
    manifest = request.manifest
    directory = agent_upload_dir(manifest.agent_id, create=False).resolve()
    with write_transaction(pool) as conn:
        storage.lock_agent(conn, manifest.agent_id)
        row = conn.execute(
            "SELECT request, storage_directory FROM upload_delivery_copies "
            "WHERE batch_id=%s AND storage_machine=%s AND unit_home=%s",
            (manifest.batch_id, unit.machine, unit.home),
        ).fetchone()
        if row is not None and (row[0] != request.model_dump() or row[1] != str(directory)):
            raise UploadDeliveryConflictError(
                "copy identity already binds another manifest or native root"
            )
        storage.check_quota(
            conn,
            manifest.agent_id,
            directory,
            unit.machine,
            sum(item.size for item in manifest.objects),
            len(manifest.objects),
            MAX_AGENT_UPLOAD_BYTES,
            MAX_AGENT_UPLOAD_FILES,
            incoming_manifest=manifest,
        )
        if row is None:
            conn.execute(
                "INSERT INTO upload_delivery_copies "
                "(batch_id,unit_home,agent_id,request,manifest,storage_machine,storage_directory) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    manifest.batch_id,
                    unit.home,
                    manifest.agent_id,
                    Jsonb(request.model_dump()),
                    Jsonb(manifest.model_dump()),
                    unit.machine,
                    str(directory),
                ),
            )
        else:
            # Lost/deleted ready files need durable admission again before fill;
            # a historical ready marker cannot waive other writers' quota.
            conn.execute(
                "UPDATE upload_delivery_copies SET ready_at=NULL WHERE batch_id=%s "
                "AND storage_machine=%s AND unit_home=%s",
                (manifest.batch_id, unit.machine, unit.home),
            )
    # Ready evidence is historical: verify actual files before returning ANY proof.
    # Fetch only absent objects; a conflicting present final is never overwritten.
    deadline = time.monotonic() + 120.0
    contents: list[bytes] = []
    for item in manifest.objects:
        path = storage.object_path(directory, manifest, item)
        if path.exists() or path.is_symlink():
            storage.verify(path, item)
            contents.append(path.read_bytes())
        else:
            url = (
                f"{gateway_api_base().rstrip('/')}/api/keyed/v1/agents/{manifest.agent_id}"
                f"/uploads/{manifest.batch_id}/objects/{item.ordinal}"
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("immutable upload copy exhausted its bounded network round")
            response = http_get(url, headers=gateway_auth_headers(), timeout=min(30.0, remaining))
            response.raise_for_status()
            contents.append(response.content)
    storage.publish(directory, manifest, contents)
    storage.verify_all(directory, manifest)
    with write_transaction(pool) as conn:
        conn.execute(
            "UPDATE upload_delivery_copies SET ready_at=now() WHERE batch_id=%s "
            "AND storage_machine=%s AND unit_home=%s",
            (manifest.batch_id, unit.machine, unit.home),
        )
    return CopyProof(
        version=1,
        target=unit,
        manifest_hash=manifest.fingerprint(),
        directory=str(directory / NAMESPACE / manifest.batch_id),
    )
