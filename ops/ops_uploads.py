"""`upload_receive` op — pull an uploaded file onto this runner's disk.

The gateway stores every file upload on its own disk (`~/Downloads/
AvaAgent-<id>/`) and serves it back at `/api/agents/<id>/uploads/<name>`.
An agent that runs on a remote runner cannot read the gateway's local disk,
so after saving the gateway dispatches this op: the runner fetches the file
over HTTP (cluster-secret bearer, the same uniform path every runner ->
gateway dial uses) and writes it into its own `~/Downloads/AvaAgent-<id>/`.
The op result carries this host's absolute path — the gateway's notification
message then tells the agent where the file physically landed.

The fetch is a plain GET of the upload URL, NOT `shared.uploads.fetch_upload_b64`:
that helper is image-only (it base64-inlines for the multimodal claim path).
This op must accept arbitrary file types.
"""

from __future__ import annotations

import logging

from ops.rpc_schemas import UploadReceivePayload, UploadReceiveResult
from shared.config import settings
from shared.http_dial import get as http_get
from shared.machine import gateway_api_base
from shared.uploads import agent_upload_dir, sanitize_upload_name

_log = logging.getLogger(__name__)


def upload_receive_op(payload: UploadReceivePayload) -> UploadReceiveResult:
    """Fetch one upload from the gateway and write it into this host's local
    uploads dir; return the local absolute path.

    Raises:
        OSError: the gateway is unreachable, the file is gone, or the write
            failed — surfaced as a 'failed' op result the gateway degrades
            (it still delivers the notification with the gateway-side URL).
    """
    from shared.cluster_auth import bearer_header

    name = sanitize_upload_name(payload.name)
    dest = agent_upload_dir(payload.agent_id)
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / name

    url = f"{gateway_api_base().rstrip('/')}/api/agents/{payload.agent_id}/uploads/{name}"
    secret = settings.data_plane.cluster_secret
    try:
        resp = http_get(url, headers=bearer_header(secret) if secret else {}, timeout=60.0)
        resp.raise_for_status()
    except Exception as exc:  # httpx.HTTPError + friends
        raise OSError(f"pull upload {url!r} failed: {exc}") from exc

    target.write_bytes(resp.content)
    _log.info(
        "upload_receive: pulled %s -> %s (%d bytes)",
        url,
        target,
        len(resp.content),
    )
    return UploadReceiveResult(path=str(target))
