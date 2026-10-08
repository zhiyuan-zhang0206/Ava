"""`upload_receive` op — pull an uploaded file onto this runner's disk.

The gateway stores every file upload on its own disk (`~/Downloads/
AvaAgent-<id>/`) and serves it back at `/api/agents/<id>/uploads/<name>`.
An agent that runs on a remote runner cannot read the gateway's local disk,
so after saving the gateway dispatches this op: the runner fetches the file
over HTTP (cluster-secret bearer, the same uniform path every runner ->
gateway dial uses) and writes it into its own `~/Downloads/AvaAgent-<id>/`.
The op result carries this host's absolute path — the gateway's notification
message then tells the agent where the file physically landed.

The fetch is a plain GET of the upload URL, NOT `base.agents.uploads.fetch_upload_b64`:
that helper is image-only (it base64-inlines for the multimodal claim path).
This op must accept arbitrary file types.
"""

from __future__ import annotations

import logging
from pathlib import Path

from psycopg_pool import ConnectionPool

from base.agents.upload_delivery import storage
from base.agents.upload_delivery.models import NAMESPACE
from base.agents.uploads import (
    MAX_AGENT_UPLOAD_BYTES,
    MAX_AGENT_UPLOAD_FILES,
    agent_upload_dir,
    sanitize_upload_name,
)
from base.cluster.machine import gateway_api_base, machine_name
from base.db.transaction import write_transaction
from base.host.net.http_dial import get as http_get
from base.host.private_storage import write_private_bytes
from ops.rpc_schemas import UploadReceivePayload, UploadReceiveResult

_log = logging.getLogger(__name__)


def _publish(target: Path, agent_id: int, contents: bytes, pool: ConnectionPool | None) -> None:
    if pool is None:
        # Direct helper compatibility; the native dispatch always binds its pool.
        write_private_bytes(target, contents)
        return
    with write_transaction(pool) as conn:
        storage.lock_agent(conn, agent_id)
        exists = target.is_file()
        storage.check_quota(
            conn,
            agent_id,
            target.parent,
            machine_name(),
            len(contents) - (target.stat().st_size if exists else 0),
            0 if exists else 1,
            MAX_AGENT_UPLOAD_BYTES,
            MAX_AGENT_UPLOAD_FILES,
        )
        write_private_bytes(target, contents)


def upload_receive_op(
    payload: UploadReceivePayload, *, pool: ConnectionPool | None = None
) -> UploadReceiveResult:
    """Fetch one upload from the gateway and write it into this host's local
    uploads dir; return the local absolute path. Native dispatch supplies its
    pool for physical quota admission after HTTP; direct payload-only calls
    retain their historical filesystem-only behavior.

    Raises:
        OSError: the gateway is unreachable, the file is gone, or the write
            failed — surfaced as a 'failed' op result the gateway degrades
            (it still delivers the notification with the gateway-side URL).
    """
    from base.cluster.machine import gateway_auth_headers

    name = sanitize_upload_name(payload.name)
    if name == NAMESPACE:
        raise ValueError("upload filename belongs to the delivered batch namespace")
    dest = agent_upload_dir(payload.agent_id)
    target = dest / name

    url = f"{gateway_api_base().rstrip('/')}/api/agents/{payload.agent_id}/uploads/{name}"
    try:
        resp = http_get(url, headers=gateway_auth_headers(), timeout=60.0)
        resp.raise_for_status()
    except Exception as exc:  # httpx.HTTPError + friends
        raise OSError(f"pull upload {url!r} failed: {exc}") from exc

    _publish(target, payload.agent_id, resp.content, pool)
    _log.info(
        "upload_receive: pulled %s -> %s (%d bytes)",
        url,
        target,
        len(resp.content),
    )
    return UploadReceiveResult(path=str(target))
