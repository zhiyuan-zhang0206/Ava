"""Typed gateway operations for the process-local SDK.

The SDK resolves its existing process context; the base gateway client owns
HTTP, retry policy and wire-error translation for explicit contexts.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, Literal
from uuid import UUID

import meiva.sdk_surface.agent_identity
from base.agents.context import MeivaContext
from base.agents.context.gateway_client import launch_retry as _launch_retry
from base.agents.context.gateway_client import transport as _transport
from base.api_contracts.agent_contract import GatewayUnavailable as GatewayUnavailable
from base.log import logger
from meiva.sdk_surface import process_context


def _ctx(context: MeivaContext | None) -> MeivaContext:
    return context if context is not None else process_context.current_context()


def get(
    path: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: _transport.TransportTimeout | None = None,
    max_retries: int | None = None,
    context: MeivaContext | None = None,
) -> _transport.TransportResponse:
    """Read a gateway route using an explicit context or the SDK process context.

    Keep the context's HTTP client, timeout and bounded retry policy. A shared
    host must supply its context; it cannot borrow an ambient agent identity.
    """
    return _transport.get(
        path, params=params, timeout=timeout, max_retries=max_retries, context=_ctx(context)
    )


@contextmanager
def use_client(client: Any) -> Generator[Any]:
    """Route every SDK call in the block through *client* — a FastAPI `TestClient` for an
    in-process gateway, or any `httpx.Client` — then put back whichever client was there before."""
    with process_context.current_context().clients.using_gateway(client):
        yield client


def get_launch_attempt(agent_id: int) -> UUID:
    """Read the current metadata attempt; an absent old field is not evidence."""
    _launch_retry.validate_agent_id(agent_id, operation="launch attempt observation")
    return _launch_retry.get_launch_attempt(agent_id, context=_ctx(None))


def retry_launch(
    agent_id: int,
    *,
    idempotency_key: str,
    expected_prior_attempt_id: str | UUID,
) -> int:
    """Retry one observed attempt through the fixed guarded operation."""
    _launch_retry.validate_retry_admission(key=idempotency_key, prior=expected_prior_attempt_id)
    _launch_retry.validate_agent_id(agent_id, operation="guarded launch retry")
    return _launch_retry.retry_launch(
        agent_id,
        idempotency_key=idempotency_key,
        expected_prior_attempt_id=expected_prior_attempt_id,
        context=_ctx(None),
    )


def spawn(
    *,
    spawner: str,
    prompt: str | None,
    fork_from: int | None,
    prompt_source: str,
    machine: str | None = None,
    config: dict[str, object] | None = None,
    label: str | None = None,
    idempotency_key: str,
) -> int:
    """Create or fork an agent with a caller-owned immutable identity."""
    from base.api_contracts.idempotency import validate_idempotency_key

    idempotency_key = validate_idempotency_key(idempotency_key)
    body: dict = {"spawner": spawner}
    if prompt is not None:
        # prompt_source is schema-required only when prompt is given (the source concept only exists when non-empty)
        body["prompt"] = prompt
        body["prompt_source"] = prompt_source
    if fork_from is not None:
        body["fork_from"] = fork_from
    if machine is not None:
        body["machine"] = machine
    if config is not None:
        body["config"] = config
    if label is not None:
        body["label"] = label
    # Every retry keeps the caller's key, body and fixed admission path, so an
    # ambiguous outcome is retried and the server replays the original creation.
    resp = _transport.post(
        "/api/keyed/v1/agents", body, idempotency_key=idempotency_key, context=_ctx(None)
    )
    _transport.raise_from_response(resp)
    data = resp.json()
    normalized = data.get("config_normalized")
    if normalized:
        # The spawner-visible receipt for a withdrawal settlement (task #4306):
        # the gateway stored the registered fallback, but this template still
        # names the withdrawn id — fix it at the source.
        logger.warning(
            "spawn config_overlay llm_model {requested!r} is withdrawn; the "
            "agent was stored on the registered fallback {resolved!r} — update "
            "the template (task #4306)",
            event="spawn_config_normalized",
            requested=normalized.get("requested"),
            resolved=normalized.get("resolved"),
        )
    return int(data["id"])


def send_message(
    agent_id: int,
    *,
    content: str | list[dict[str, object]],
    source: str,
    completion_notice: bool = False,
) -> None:
    """Deliver with bounded same-key HTTP retries; failures return to the caller.

    The gateway's durable inbound key prevents duplicate acceptance after response
    loss. There is no sender journal or delivery after this invocation exits.
    """
    from uuid import uuid4

    import httpx

    response = _transport.post(
        f"/api/agents/{agent_id}/messages",
        {
            "content": content,
            "source": source,
            **({"completion_notice": True} if completion_notice else {}),
        },
        timeout=httpx.Timeout(120.0),
        idempotency_key=str(uuid4()),
        context=_ctx(None),
    )
    _transport.raise_from_response(response)


def send_system_note(
    agent_id: int,
    *,
    content: str,
    note_tag: str,
    source: str,
    task_id: int | None,
    resurrect: bool,
    idempotency_key: str,
) -> int:
    """POST /api/agents/{id}/system-note — deliver a framework system note.

    Returns the durable inbound id; renders as a system note (no sender
    prefix), waking a live agent, reviving a terminated one when `resurrect`
    is set (same 120 s per-call timeout as send_message).
    """
    import httpx

    from base.api_contracts.idempotency import validate_idempotency_key

    idempotency_key = validate_idempotency_key(idempotency_key)

    body: dict[str, str | bool | int] = {
        "content": content,
        "note_tag": note_tag,
        "source": source,
        "resurrect": resurrect,
    }
    if task_id is not None:
        body["task_id"] = task_id
    resp = _transport.post(
        f"/api/agents/{agent_id}/system-note",
        body,
        timeout=httpx.Timeout(120.0),
        idempotency_key=idempotency_key,
        context=_ctx(None),
    )
    _transport.raise_from_response(resp)
    return int(resp.json()["inbound_id"])


def get_last_message(agent_id: int, caller: str) -> str | None:
    """GET /api/agents/{id}/last-message → the agent's most recent AI turn text."""
    resp = _transport.get(
        f"/api/agents/{agent_id}/last-message", params={"caller": caller}, context=_ctx(None)
    )
    _transport.raise_from_response(resp)
    return resp.json()["text"]


def get_neighbors(agent_id: int, *, depth: int, limit: int) -> list[dict]:
    """GET /api/agents/{id}/neighbors → list of neighbor dicts.

    Each dict has id / label / status / depth / score, strongest first
    (order preserved from the gateway).
    """
    resp = _transport.get(
        f"/api/agents/{agent_id}/neighbors",
        params={"depth": depth, "limit": limit},
        context=_ctx(None),
    )
    _transport.raise_from_response(resp)
    return resp.json()["neighbors"]


def get_ancestors(agent_id: int) -> list[dict]:
    """GET /api/agents/{id}/neighbors → the `ancestors` rows: the spawn/fork
    chain above `agent_id`, nearest ancestor first (the gateway walks to the
    top, so the neighbors `depth`/`limit` params do not apply). Same dict
    shape as get_neighbors."""
    resp = _transport.get(
        f"/api/agents/{agent_id}/neighbors", params={"depth": 1, "limit": 20}, context=_ctx(None)
    )
    _transport.raise_from_response(resp)
    return resp.json()["ancestors"]


# The born-chain read runs at every context establishment, on the agent
# host's event loop. One attempt with a tight bound is deliberate: a gateway
# hiccup skips the inherited note for this window (the next establishment
# retries — the chain is immutable, so nothing is lost) instead of parking
# the agent's birth behind seconds of retry backoff it cannot use.
_BORN_CHAIN_TIMEOUT_S = 5.0


def get_born_chain(agent_id: int, *, context: MeivaContext | None = None) -> list[dict]:
    """GET /api/agents/{id}/born-chain → the `ancestors` rows: the immutable
    birth chain above `agent_id`, nearest ancestor first (1 = direct birth
    parent). Each dict carries agent_id / label / status / machine / depth.

    Unlike `get_ancestors`, this route reads no tie graph — no neighbor
    ranking, no Loki live tail — so turn-path callers (the inherited-memory
    context note at window establishment) can resolve the chain cheaply.
    The call itself is one-shot with a tight timeout (see
    `_BORN_CHAIN_TIMEOUT_S`); callers degrade on `GatewayUnavailable`."""
    import httpx

    resp = _transport.get(
        f"/api/agents/{agent_id}/born-chain",
        timeout=httpx.Timeout(_BORN_CHAIN_TIMEOUT_S),
        max_retries=1,
        context=_ctx(context),
    )
    _transport.raise_from_response(resp)
    return resp.json()["ancestors"]


def list_agents(
    *,
    scope: str = "live",
    query: str = "",
    before_id: int | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Read one bounded directory page; the caller owns cursor traversal."""
    params: dict[str, str | int] = {"scope": scope, "query": query, "limit": limit}
    if before_id is not None:
        params["before_id"] = before_id
    resp = _transport.get("/api/agents", params=params, context=_ctx(None))
    _transport.raise_from_response(resp)
    return resp.json()


def patch_label(agent_id: int, label: str, *, source: Literal["user", "self"] = "self") -> None:
    """Set an agent's label through the gateway; `""` resets it to unset.

    The label sticks (the background labeler no longer renames it), the change is
    audited as `label_change` with `source`, and every client's view refreshes live.
    """
    resp = _transport.patch(
        f"/api/agents/{agent_id}", json={"label": label, "source": source}, context=_ctx(None)
    )
    _transport.raise_from_response(resp)


def get_agent(agent_id: int) -> dict[str, Any]:
    """Read an agent directly, independently of directory scope or pagination."""
    resp = _transport.get(f"/api/agents/{agent_id}", context=_ctx(None))
    _transport.raise_from_response(resp)
    return resp.json()


def terminate(
    agent_id: int,
    *,
    source: str | None = None,
    message: str | None = None,
    force: bool = False,
    kill_all_shell_sessions: bool = False,
) -> dict:
    """POST /api/agents/{id}/terminate → response dict.

    `status` is "enqueued" / "already_terminated"; `open_tasks` carries what
    the agent still owned as it went down, if anything; `shell_sessions`
    carries what a requested shell-session kill did.

    An omitted source requires the established actor identity. Callers without
    an agent or system identity must supply an explicit source.

    force=True requests interruption. Hosted force returns "enqueued" while
    the original host drains actual work; acceptance is not observed exit.

    message, when present, is retained for the agent's next resurrection while
    termination proceeds without waiting for another response.

    kill_all_shell_sessions=True also kills every shell session the agent owns
    on its home machine. Sent only when set.
    """
    body: dict = {}
    if source is not None:
        body["source"] = source
    else:
        body["source"] = meiva.sdk_surface.agent_identity.require_actor()
    if message is not None:
        body["message"] = message
    if force:
        body["force"] = True
    if kill_all_shell_sessions:
        body["kill_all_shell_sessions"] = True
    resp = _transport.post(f"/api/agents/{agent_id}/terminate", body, context=_ctx(None))
    _transport.raise_from_response(resp)
    return resp.json()


def restart(agent_id: int, *, source: str | None = None) -> str:
    """POST /api/agents/{id}/restart → status string.

    An omitted source requires the established actor identity. Callers without
    an agent or system identity must supply an explicit source.
    """
    body: dict = {}
    if source is not None:
        body["source"] = source
    else:
        body["source"] = meiva.sdk_surface.agent_identity.require_actor()
    resp = _transport.post(f"/api/agents/{agent_id}/restart", body, context=_ctx(None))
    _transport.raise_from_response(resp)
    return resp.json()["status"]


def resurrect(agent_id: int, *, prompt: str, resurrected_by: str | None = None) -> str:
    """POST /api/agents/{id}/resurrect -> status string.

    An omitted resurrected_by requires the established actor identity. Callers
    without an agent or system identity must supply explicit provenance.

    prompt is required -- a resurrected agent needs to know why it
    was woken up and what to do.
    """
    body: dict = {"prompt": prompt}
    if resurrected_by is not None:
        body["resurrected_by"] = resurrected_by
    else:
        body["resurrected_by"] = meiva.sdk_surface.agent_identity.require_actor()
    resp = _transport.post(f"/api/agents/{agent_id}/resurrect", body, context=_ctx(None))
    _transport.raise_from_response(resp)
    return resp.json()["status"]


def register_page(
    agent_id: int,
    *,
    name: str,
    port: int,
    host: str,
    title: str | None,
    serve_dir: str | None = None,
    ttl_seconds: int | None = None,
) -> dict:
    """POST /api/keyed/v1/agents/{id}/pages → PageRow dict (id/name/port/title/...).

    One key per call: transport-level retries share it, so the gateway
    replays the original acceptance instead of replacing a newer page.

    `serve_dir`: directory the page server serves — recorded so agent boot
    can re-serve a dead page server after resurrect/restart. Only
    serve() sets it; show() leaves it None.
    """
    body: dict = {"name": name, "port": port, "host": host}
    if title is not None:
        body["title"] = title
    if serve_dir is not None:
        body["serve_dir"] = serve_dir
    if ttl_seconds is not None:
        body["ttl_seconds"] = ttl_seconds
    resp = _transport.post(
        f"/api/keyed/v1/agents/{agent_id}/pages",
        body,
        idempotency_key=uuid.uuid4().hex,
        context=_ctx(None),
    )
    _transport.raise_from_response(resp)
    return resp.json()


def close_page(agent_id: int, name: str) -> bool:
    """Close the agent's open page `name`; False when no such page is open.

    Observes the open page list first, then closes exactly that registry row
    through POST /api/keyed/v1/agents/{id}/pages/{name}/close. A registration
    that replaces the observed row in between fails the close with 409
    (httpx.HTTPStatusError) instead of closing the newer page.
    """
    observed = next((p for p in list_open_pages(agent_id) if p["name"] == name), None)
    if observed is None:
        return False
    resp = _transport.post(
        f"/api/keyed/v1/agents/{agent_id}/pages/{name}/close",
        {"expected_page_id": observed["id"]},
        idempotency_key=uuid.uuid4().hex,
        context=_ctx(None),
    )
    _transport.raise_from_response(resp)
    return True


def list_open_pages(agent_id: int) -> list[dict]:
    """GET /api/agents/{id}/pages → the agent's open PageRow dicts."""
    resp = _transport.get(f"/api/agents/{agent_id}/pages", context=_ctx(None))
    _transport.raise_from_response(resp)
    return list(resp.json())


def list_machines() -> list[dict]:
    """GET /api/cluster/machines → list of {name, description, live} dicts."""
    resp = _transport.get("/api/cluster/machines", context=_ctx(None))
    _transport.raise_from_response(resp)
    return list(resp.json())
