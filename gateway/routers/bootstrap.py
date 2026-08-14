"""Bootstrap endpoint — GET /api/bootstrap.

Returns cluster-common config for an agent-runner to load into its environment
at enroll / start. The endpoint serves cluster secrets, so it requires
the cluster secret as a bearer token (`Authorization: Bearer <AVA_CLUSTER_SECRET>`)
— reachability is not trust. A no-secret cluster has no credential to present
and no remote runner to protect (its gateway binds loopback), so the endpoint
serves unauthenticated then. A single box never dials it (the gateway and its
agents share the host); only an enrolling agent-runner does, and it presents the
secret.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query

from shared import config
from shared.cluster_auth import verify_bearer

router = APIRouter()


@router.get("/api/bootstrap")
def get_bootstrap(
    authorization: str | None = Header(default=None),
    role: str | None = Query(default=None),
) -> dict[str, str]:
    """Return cluster-common config ({ENV_ALIAS: value}, unmasked) for an
    agent-runner to load into its environment.

    `role` selects the credential projection: `runner` serves the least-
    privilege `ava_runner` AVA_DB_URL (the role's own password, carried inside
    the URL — never a standalone key), anything else serves the main identity
    (Task #1236; see shared.config.bootstrap_config_values).

    Raises:
        HTTPException: 401 when the request does not carry
            `Authorization: Bearer <cluster secret>` (a no-secret cluster serves
            without auth — there is no credential to require); 400 for an
            unknown role value, and when the runner projection is requested on
            a cluster whose ava_runner credential is not provisioned yet.
    """
    secret = config.settings.data_plane.cluster_secret
    if secret and not verify_bearer(authorization, secret):
        raise HTTPException(status_code=401, detail="cluster secret required")
    try:
        return config.bootstrap_config_values(role=role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
