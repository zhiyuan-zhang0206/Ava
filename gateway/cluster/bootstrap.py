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
bootstrap field). A unit's login and API token arrive only in the capability
bundle the gateway operator issues for that unit
(`ava cluster db-authority issue-unit`). Both are the write generation's,
shared by every runner unit, and the bundle's telemetry token is the
cluster's.

The route is control-plane (`base.api_contracts.contracts`): a held gateway
still serves it, since a runner started under a hold joins through it and every
runner process resolves its config from it before the hold is released. The
pause exemption changes neither the authentication nor the payload.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request

from base import config
from base.config.service_read import plugin_bootstrap_config
from gateway.http.auth.request_principal import cluster_credential

router = APIRouter()


@router.get("/api/bootstrap")
def get_bootstrap(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, str]:
    """Return cluster-common config ({ENV_ALIAS: value}, unmasked) for an
    agent-runner to load into its environment.

    `AVA_DB_URL` is the credential-free endpoint; no database credential is
    served. The admin credentials remain gateway-local (see
    base.config.bootstrap_config_values).

    Raises:
        HTTPException: 401 when the request carries neither the active write
            generation's machine API token nor the cluster secret as its bearer
            (a no-secret cluster serves without auth — there is no credential
            to require); 400 when the gateway configuration has no database
            endpoint.
    """
    secret = config.settings.data_plane.cluster_secret
    if (
        secret
        and cluster_credential(
            authorization, secret, cache=request.app.state.machine_token_acceptance
        )
        is None
    ):
        raise HTTPException(status_code=401, detail="machine API token required")
    try:
        return request.app.state.config_authority.bootstrap_config_values(
            provider_key_envs=(
                binding.key_env for binding in request.app.state.catalog.bindings.values()
            ),
            plugin_cluster_config=plugin_bootstrap_config(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
