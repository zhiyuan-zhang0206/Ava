"""The HTTP step shared by decision providers: one authenticated JSON POST, no retries.

Retrying is the caller's decision (``decide()`` is a pre-flight check or a gate, and a caller
that wants a second attempt can call it again), so a transport failure or a non-success status
surfaces as ``DecisionRequestError`` after exactly one request.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import httpx

from base.lm.catalog.decide.types import DecisionRequestError, DecisionResponseError
from base.lm.catalog.provider_contract import DecisionBuildContext

_ERROR_BODY_CHARS = 300


async def post_json(
    ctx: DecisionBuildContext, *, url: str, api_key: str, payload: Mapping[str, object]
) -> dict[str, Any]:
    """POST ``payload`` as JSON with a bearer key; return the decoded JSON object."""
    try:
        async with httpx.AsyncClient(transport=ctx.transport, timeout=ctx.timeout) as client:
            response = await client.post(
                url, json=payload, headers={"Authorization": f"Bearer {api_key}"}
            )
    except httpx.HTTPError as exc:
        raise DecisionRequestError(
            f"{ctx.model}: request to {url} failed: {type(exc).__name__}: {exc}"
        ) from exc
    if not response.is_success:
        raise DecisionRequestError(
            f"{ctx.model}: HTTP {response.status_code} from {url}: "
            f"{response.text[:_ERROR_BODY_CHARS]}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise DecisionResponseError(f"{ctx.model}: response from {url} is not JSON") from exc
    if not isinstance(body, dict):
        raise DecisionResponseError(f"{ctx.model}: response from {url} is not a JSON object")
    return cast(dict[str, Any], body)
