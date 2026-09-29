"""Shared result-read boundary for evaluation-isolated agent callers."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request


def _caller_agent_id(caller: str) -> int | None:
    """Return the agent id in an ``agent:<id>`` caller marker, if present."""
    if not caller.startswith("agent:"):
        return None
    try:
        return int(caller.removeprefix("agent:"))
    except ValueError:
        return None


def caller_eval_isolation(pool: Any, caller_agent_id: int) -> bool:
    """Whether a caller's stored eval configuration denies result reads."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT "
            "((config_overlay ->> 'eval_isolation')::boolean IS TRUE "
            "OR (birth_config ->> 'eval_isolation')::boolean IS TRUE) "
            "FROM agents_meta WHERE id = %s",
            (caller_agent_id,),
        )
        row = cur.fetchone()
    return bool(row and row[0])


def deny_isolated_result_read(request: Request) -> None:
    """Reject an eval-isolated agent caller before it can read run artifacts.

    ``caller`` intentionally comes straight from the query string rather than a
    declared FastAPI parameter. It is an SDK attribution marker, not an API
    input the frontend or generated OpenAPI clients need to know about.
    """
    caller_agent_id = _caller_agent_id(request.query_params.get("caller", ""))
    if caller_agent_id is None or not caller_eval_isolation(
        request.app.state.db_pool, caller_agent_id
    ):
        return
    surface = "last-message" if request.url.path.endswith("/last-message") else "result"
    raise HTTPException(
        status_code=403,
        detail=f"caller agent {caller_agent_id} is eval-isolated: {surface} reads are denied",
    )
