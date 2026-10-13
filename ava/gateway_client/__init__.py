"""SDK ↔ Gateway HTTP client.

The package door is the client — typed gateway operations the SDK namespaces
call. `transport` is the HTTP layer under it: the process-wide httpx client,
retry/backoff, and wire-error translation. Other `ava` modules that read a raw
gateway route use `transport` directly.

The agent process's `ava.agents.*` no longer directly connects to the DB —
three gateway ops (spawn / send_message / get_last_message)
all go through this module calling gateway HTTP routes. See the
"error-wire protocol" section at the top of `base/api_contracts/agent_contract.py`.

Design trade-offs:
- **Synchronous httpx.Client**: agent code is sync; introducing async
  isn't worth the cost. An HTTP round-trip on localhost is a few ms, not
  in the same order of magnitude as DB writes.
- **module-level singleton client**: process-wide shared connection pool.
- **Error mapping**:
  - Transient failures — the `httpx.TransportError` family (connect/read/
    write/pool timeout, connection refused, DNS failure, protocol error,
    etc.) and HTTP 429/5xx responses → retried up to 3 times with bounded
    exponential backoff + per-agent jitter; on full failure, raise
    `GatewayUnavailable` (transport) or surface the wire error /
    `HTTPStatusError` (HTTP)
  - HTTP non-2xx with wire-contract JSON body → go through
    `raise_from_response`, reverse-lookup `EXCEPTION_BY_REASON` by
    `reason` field to rebuild AvaAgentError subclass
  - HTTP non-2xx with non-JSON body / missing reason field (FastAPI default
    500, `ErrorReason(...)` ValueError, etc.) → fall through to
    `resp.raise_for_status()` raising `httpx.HTTPStatusError`; fail fast
    so the caller sees the full stack trace; don't swallow. The status is
    raised cleanly — outside the parse's except handler — so the original
    status code is never masked by a chained `KeyError: 'reason'` (task #1205)
- **Retry policy**: transient failures — the `httpx.TransportError` family
  AND HTTP 429/5xx responses — are retried up to 3 times with bounded
  exponential backoff (1s → 2s → 4s → 8s cap) plus a deterministic per-agent
  jitter offset (the heartbeat-daemon de-phasing pattern: a correlated
  outage hits every agent at the same moment, and a fleet-wide identical
  schedule would retry in lockstep). 4xx stay non-retried — the wire
  `reason` is authoritative application semantics (AgentNotFound,
  MachineNotRegistered, ...); retrying cannot change the result.
  Retrying is gated on idempotency: a request marked `idempotent=False`
  (spawn / send_message — a re-send can duplicate the effect) is retried
  only on connect-family failures, which provably never reached the server;
  a ReadTimeout or an HTTP 5xx on such a request means "outcome unknown"
  and is surfaced immediately instead of re-sent.
  Scenarios retry covers:
  - Gateway is restarting (ConnectError → wait for new process to ready)
  - Gateway just exited, residual TCP connections (ConnectError / RemoteProtocolError)
  - Transient network jitter (ReadTimeout)
  - Backend blip behind the gateway (HTTP 429/500/502/503/504 — e.g. the
    memory-indexer 500 that crashed an agent's graph before this policy,
    task #960)
  Scenarios retry doesn't cover:
  - HTTP 4xx (AgentNotFound, MachineNotRegistered, etc.) — application-
    layer semantics; retrying doesn't change the result
  - ReadTimeout / HTTP 5xx on a non-idempotent request — the server may
    have acted on it; re-sending duplicates the effect (spawn → twin,
    send_message → duplicate inbound)
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Any, Literal, NamedTuple

import ava.sdk_surface.agent_identity
from ava.gateway_client.launch_retry import get_launch_attempt as get_launch_attempt
from ava.gateway_client.launch_retry import retry_launch as retry_launch
from ava.gateway_client.transport import (
    MEMORY_SEARCH_MAX_RETRIES,
    get,
    memory_search_timeout,
    post,
    raise_from_response,
)
from ava.gateway_client.transport import (
    MEMORY_SEARCH_TIMEOUT_MARGIN_S as MEMORY_SEARCH_TIMEOUT_MARGIN_S,
)
from ava.gateway_client.transport import (
    patch as patch,
)
from base.agents.context import AvaContext
from base.api_contracts.agent_contract import GatewayUnavailable as GatewayUnavailable
from base.api_contracts.memory_search import MemorySearchPurpose
from base.log import logger


class MemorySearchResult(NamedTuple):
    """One memory-search hit as the SDK boundary sees it.

    The single client-side shape for a search result, so every consumer
    (`ava.memory.search`, passive recall) reads the same fields instead of
    re-parsing the wire dict — a new field added here flows to all of them.

    path: memory-pool-relative path of the note.
    description: the note's frontmatter `description`, extracted server-side;
        empty string when the note has none (never synthesized from title/body).
    tags: the note's frontmatter `tags`, including its `type/<x>`; empty when it
        has none. What lets a caller weigh a hit by the kind of note it is, not
        just by how it scored.
    score: the ranking model's probability (0..1) that `chunk` is relevant to
        the query.
    chunk: the text of the note's best-matching chunk, or its description line.
    """

    path: str
    description: str
    tags: tuple[str, ...]
    score: float
    chunk: str


def memory_search(
    query: str,
    k: int,
    *,
    purpose: MemorySearchPurpose,
    exclude: Collection[str] = (),
    timeout: float | None = None,
    attempts: int = MEMORY_SEARCH_MAX_RETRIES,
    context: AvaContext | None = None,
) -> list[MemorySearchResult]:
    """POST /api/memory/search → list of `MemorySearchResult`, most relevant first.

    The gateway embeds the query, retrieves candidate chunks from the memory-search
    service, and has a decision model score each chunk's relevance (see
    `MemorySearchResult`). Returns relative paths with frontmatter
    descriptions extracted server-side, so callers do not need to
    re-read every file to get a summary.

    Semantically a read (idempotent): a transient backend 5xx (the
    2026-08-07 memory-indexer 500 class) is retried once with backoff +
    jitter before surfacing, so a blip self-heals instead of reaching the
    caller — and a persistent failure (the gateway's own deadline 503, a
    congested gate) surfaces as `IndexerUnavailable` without stacking
    attempts behind it.

    `purpose` says why the query was issued ("recall": passive recall, the query is the recent
    conversation; "search": an agent's own search; "dedup": the duplicate check on a note just
    written, the query is that note), which selects the question the gateway's ranking model
    answers.

    `exclude` lists memory-pool-relative note paths the gateway leaves out before ranking: they
    cost no ranking request and take no candidate slot. At most `k` results come back, and never
    more than the notes the gateway ranks per search.

    `timeout` is how long one attempt may take (seconds). It travels in the request, so the gateway
    stops ranking when it passes: a call whose caller has stopped waiting must not keep spending
    ranking requests. The HTTP timeout is `timeout` plus a 3s margin, so the gateway's answer
    (`IndexerUnavailable`) arrives before the client reads out and reports `GatewayUnavailable`.
    The default is no request deadline, and the gateway's own search deadline plus the margin
    (18s at default settings) as the HTTP timeout.

    `attempts` is how many times a transient failure is tried in all (default 2: one retry). A
    caller that gives up on its own deadline passes 1: a retry would start a second search after
    the caller has stopped waiting for the first.
    """
    import httpx

    body: dict[str, object] = {"query": query, "k": k, "purpose": purpose}
    if exclude:
        body["exclude"] = sorted(exclude)
    if timeout is not None:
        body["deadline_seconds"] = timeout
    resp = post(
        "/api/memory/search",
        body,
        timeout=(
            httpx.Timeout(timeout + MEMORY_SEARCH_TIMEOUT_MARGIN_S)
            if timeout is not None
            else memory_search_timeout(context)
        ),
        max_retries=attempts,
        context=context,
    )
    raise_from_response(resp)
    return [
        MemorySearchResult(
            path=r["path"],
            description=r["description"],
            tags=tuple(r["tags"]),
            score=float(r["score"]),
            chunk=r["chunk"],
        )
        for r in resp.json()["results"]
    ]


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
    resp = post(
        "/api/keyed/v1/agents",
        body,
        idempotency_key=idempotency_key,
    )
    raise_from_response(resp)
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

    response = post(
        f"/api/agents/{agent_id}/messages",
        {
            "content": content,
            "source": source,
            **({"completion_notice": True} if completion_notice else {}),
        },
        timeout=httpx.Timeout(120.0),
        idempotency_key=str(uuid4()),
    )
    raise_from_response(response)


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
    resp = post(
        f"/api/agents/{agent_id}/system-note",
        body,
        timeout=httpx.Timeout(120.0),
        idempotency_key=idempotency_key,
    )
    raise_from_response(resp)
    return int(resp.json()["inbound_id"])


def get_last_message(agent_id: int, caller: str) -> str | None:
    """GET /api/agents/{id}/last-message → the agent's most recent AI turn text."""
    resp = get(f"/api/agents/{agent_id}/last-message", params={"caller": caller})
    raise_from_response(resp)
    return resp.json()["text"]


def get_neighbors(agent_id: int, *, depth: int, limit: int) -> list[dict]:
    """GET /api/agents/{id}/neighbors → list of neighbor dicts.

    Each dict has id / label / status / depth / score, strongest first
    (order preserved from the gateway).
    """
    resp = get(f"/api/agents/{agent_id}/neighbors", params={"depth": depth, "limit": limit})
    raise_from_response(resp)
    return resp.json()["neighbors"]


def get_ancestors(agent_id: int) -> list[dict]:
    """GET /api/agents/{id}/neighbors → the `ancestors` rows: the spawn/fork
    chain above `agent_id`, nearest ancestor first (the gateway walks to the
    top, so the neighbors `depth`/`limit` params do not apply). Same dict
    shape as get_neighbors."""
    resp = get(f"/api/agents/{agent_id}/neighbors", params={"depth": 1, "limit": 20})
    raise_from_response(resp)
    return resp.json()["ancestors"]


# The born-chain read runs at every context establishment, on the agent
# host's event loop. One attempt with a tight bound is deliberate: a gateway
# hiccup skips the inherited note for this window (the next establishment
# retries — the chain is immutable, so nothing is lost) instead of parking
# the agent's birth behind seconds of retry backoff it cannot use.
_BORN_CHAIN_TIMEOUT_S = 5.0


def get_born_chain(agent_id: int, *, context: AvaContext | None = None) -> list[dict]:
    """GET /api/agents/{id}/born-chain → the `ancestors` rows: the immutable
    birth chain above `agent_id`, nearest ancestor first (1 = direct birth
    parent). Each dict carries agent_id / label / status / machine / depth.

    Unlike `get_ancestors`, this route reads no tie graph — no neighbor
    ranking, no Loki live tail — so turn-path callers (the inherited-memory
    context note at window establishment) can resolve the chain cheaply.
    The call itself is one-shot with a tight timeout (see
    `_BORN_CHAIN_TIMEOUT_S`); callers degrade on `GatewayUnavailable`."""
    import httpx

    resp = get(
        f"/api/agents/{agent_id}/born-chain",
        timeout=httpx.Timeout(_BORN_CHAIN_TIMEOUT_S),
        max_retries=1,
        context=context,
    )
    raise_from_response(resp)
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
    resp = get("/api/agents", params=params)
    raise_from_response(resp)
    return resp.json()


def patch_label(agent_id: int, label: str, *, source: Literal["user", "self"] = "self") -> None:
    """Set an agent's label through the gateway; `""` resets it to unset.

    The label sticks (the background labeler no longer renames it), the change is
    audited as `label_change` with `source`, and every client's view refreshes live.
    """
    resp = patch(f"/api/agents/{agent_id}", json={"label": label, "source": source})
    raise_from_response(resp)


def get_agent(agent_id: int) -> dict[str, Any]:
    """Read an agent directly, independently of directory scope or pagination."""
    resp = get(f"/api/agents/{agent_id}")
    raise_from_response(resp)
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
        body["source"] = ava.sdk_surface.agent_identity.require_actor()
    if message is not None:
        body["message"] = message
    if force:
        body["force"] = True
    if kill_all_shell_sessions:
        body["kill_all_shell_sessions"] = True
    resp = post(f"/api/agents/{agent_id}/terminate", body)
    raise_from_response(resp)
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
        body["source"] = ava.sdk_surface.agent_identity.require_actor()
    resp = post(f"/api/agents/{agent_id}/restart", body)
    raise_from_response(resp)
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
        body["resurrected_by"] = ava.sdk_surface.agent_identity.require_actor()
    resp = post(f"/api/agents/{agent_id}/resurrect", body)
    raise_from_response(resp)
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
    resp = post(
        f"/api/keyed/v1/agents/{agent_id}/pages",
        body,
        idempotency_key=uuid.uuid4().hex,
    )
    raise_from_response(resp)
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
    resp = post(
        f"/api/keyed/v1/agents/{agent_id}/pages/{name}/close",
        {"expected_page_id": observed["id"]},
        idempotency_key=uuid.uuid4().hex,
    )
    raise_from_response(resp)
    return True


def list_open_pages(agent_id: int) -> list[dict]:
    """GET /api/agents/{id}/pages → the agent's open PageRow dicts."""
    resp = get(f"/api/agents/{agent_id}/pages")
    raise_from_response(resp)
    return list(resp.json())


def list_machines() -> list[dict]:
    """GET /api/cluster/machines → list of {name, description, live} dicts."""
    resp = get("/api/cluster/machines")
    raise_from_response(resp)
    return list(resp.json())


def list_presets() -> list[dict]:
    """GET /api/presets → list of preset dicts, ordered by name."""
    resp = get("/api/presets")
    raise_from_response(resp)
    return list(resp.json())
