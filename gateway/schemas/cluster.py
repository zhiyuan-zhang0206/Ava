"""cluster ops dispatch request (POST /api/cluster/rollout | /restart).

Split out of the former monolithic ops/schemas.py; FastAPI registers these
unchanged, so the OpenAPI codegen is byte-identical to the wire before.
"""

from pydantic import (
    BaseModel,
)


class ClusterOpRequest(BaseModel):
    """POST /api/cluster/rollout | /api/cluster/restart request body — fully
    optional; a body-less POST uses defaults.

    `origin` names the trigger, same convention as `resurrected_by`:
    default "user" (the frontend buttons), SDK passes f"agent:{my_id}".
    It heads the rollout/restart log and (rollout only) lands in the
    cluster pin's `updated_by`, so "who moved the cluster" is answerable
    from the log file or the pin row alone."""

    origin: str = "user"

    mode: str = "smooth"

    # Rollout only: start even though a deploy is in flight (overrides the
    # deploy-window check; does NOT clear a crashed rollout's update lock —
    # that is `ava cluster recover`). Ignored by /api/cluster/restart.
    force: bool = False
    """Agent-drain policy for the rollout's quiesce step: 'smooth' (default)
    waits out the longest single execute_code then force-reaps stragglers;
    'force' waits ~10s then force-reaps. Both restart every agent onto the new
    code; 'smooth' just gives them time to land cleanly first."""
