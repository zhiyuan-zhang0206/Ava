"""Bootstrap endpoint — GET /api/bootstrap.

Returns cluster-common config for an agent-runner to load into its environment
at start. The endpoint serves cluster configuration (including the Redis
runtime URL and provider keys), so it requires a bearer — reachability is not
trust: the active write generation's machine API token a unit's capability
carries (or the human cluster secret, for an operator). A no-secret cluster
has no credential to present and no remote runner to protect (its gateway
binds loopback), so the endpoint serves unauthenticated then. A single box
never dials it (the gateway and its agents share the host); only a remote unit
does.

It never serves a database login (`AVA_DB_URL` is the credential-free
endpoint) nor the human cluster secret (`AVA_CLUSTER_SECRET` is not a
bootstrap field). A unit's login and API token arrive only through the
per-unit capability the gateway operator issues
(`ava cluster db-authority issue-unit`).
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from gateway.request_principal import cluster_credential
from shared import config

router = APIRouter()


@router.get("/api/bootstrap")
def get_bootstrap(authorization: str | None = Header(default=None)) -> dict[str, str]:
    """Return cluster-common config ({ENV_ALIAS: value}, unmasked) for an
    agent-runner to load into its environment.

    `AVA_DB_URL` is the credential-free endpoint; no database credential is
    served. The admin credentials remain gateway-local (see
    shared.config.bootstrap_config_values).

    Raises:
        HTTPException: 401 when the request carries neither the active write
            generation's machine API token nor the cluster secret as its bearer
            (a no-secret cluster serves without auth — there is no credential
            to require); 400 when the gateway configuration has no database
            endpoint.
    """
    secret = config.settings.data_plane.cluster_secret
    if secret and cluster_credential(authorization, secret) is None:
        raise HTTPException(status_code=401, detail="machine API token required")
    try:
        return config.bootstrap_config_values()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
