"""SDK ↔ Gateway HTTP client (private).

The agent process's `ava.agents.*` no longer directly connects to the DB —
three gateway ops (spawn / send_message / get_last_message)
all go through this module calling gateway HTTP routes. See the
"error-wire protocol" section at the top of `shared/agents.py`.

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
    `_raise_from_response`, reverse-lookup `EXCEPTION_BY_REASON` by
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

from typing import Any, NamedTuple

import ava
import ava._boot
from ava._gateway_transport import (
    _MEMORY_SEARCH_MAX_RETRIES,
    _delete,
    _get,
    _memory_search_timeout,
    _post,
    _raise_from_response,
)
from ava._gateway_transport import (
    _MEMORY_SEARCH_TIMEOUT_MARGIN_S as _MEMORY_SEARCH_TIMEOUT_MARGIN_S,
)
from ava._gateway_transport import (
    _patch as _patch,
)
from shared.agents import AgentStatus
from shared.agents import GatewayUnavailable as GatewayUnavailable


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
        just by how it scored. A tuple, so the default is not a mutable object
        shared by every result that has no tags.
    """

    path: str
    description: str
    tags: tuple[str, ...] = ()


def memory_search(query: str, k: int, *, timeout: float | None = None) -> list[MemorySearchResult]:
    """POST /api/memory/search → list of `MemorySearchResult`.

    Gateway-side primary directly calls embedder + milvus; secondary
    forwards to primary. Returns relative paths with frontmatter
    descriptions extracted server-side, so callers do not need to
    re-read every file to get a summary.

    Semantically a read (idempotent): a transient backend 5xx (the
    2026-08-07 memory-indexer 500 class) is retried once with backoff +
    jitter before surfacing, so a blip self-heals instead of reaching the
    caller — and a persistent failure (the gateway's own deadline 503, a
    congested gate) surfaces as `IndexerUnavailable` without stacking
    attempts behind it.

    `timeout` overrides the per-attempt HTTP timeout (seconds). The default
    is the gateway's own search deadline plus a 3s margin (18s at default
    settings): keep it above `AVA_MEMORY_SEARCH_DEADLINE_SECONDS`, or the
    client reads out first and the caller sees `GatewayUnavailable` instead
    of the informative `IndexerUnavailable`.
    """
    import httpx

    resp = _post(
        "/api/memory/search",
        {"query": query, "k": k},
        timeout=httpx.Timeout(timeout) if timeout is not None else _memory_search_timeout(),
        max_retries=_MEMORY_SEARCH_MAX_RETRIES,
    )
    _raise_from_response(resp)
    return [
        MemorySearchResult(
            path=r["path"], description=r["description"], tags=tuple(r.get("tags", []))
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
    preset: str | None = None,
) -> int:
    """POST /api/agents → new agent_id."""
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
    if preset is not None:
        body["preset"] = preset
    # Non-idempotent create (doorplate: POST /api/agents = NON_IDEMPOTENT):
    # a ReadTimeout or an HTTP 5xx means the gateway may have already spawned
    # the agent (response lost, not request lost) — retrying could produce a
    # phantom-twin agent, so the retry is limited to connect-family failures
    # (task #698 G7 + task #960). Inherited from the contract — no override.
    resp = _post("/api/agents", body)
    _raise_from_response(resp)
    return int(resp.json()["id"])


def send_message(
    agent_id: int,
    *,
    content: str | list[dict[str, object]],
    source: str,
) -> None:
    """POST /api/agents/{id}/messages — deliver a chat inbound.

    `content` is either a plain string or a list of OpenAI-shaped content blocks
    (`{"type": "text", ...}` / `{"type": "image_url", "image_url": {"url": ...}}`)
    for a multimodal message; an image url must reference an upload of the
    target agent. Pure INSERT + return. Auto-resurrect on the gateway side (in
    `deliver_chat_inbound`) ensures the message always reaches a live
    agent. The caller does not inspect status — a message to any agent
    is always deliverable.

    Uses a per-call 120 s timeout (overriding the global 20 s client
    default) so a large message body or a gateway-side auto-resurrect
    does not trigger a read timeout.

    AtLeastOnceWithKey (doorplate): `client_message_id` is committed on the
    inbound row under a unique constraint. Every retry uses the same generated
    `Idempotency-Key`, so it returns that row's stable id instead of duplicating
    the message even if the first HTTP response was lost.
    """
    import httpx

    body = {"content": content, "source": source}
    resp = _post(
        f"/api/agents/{agent_id}/messages",
        body,
        timeout=httpx.Timeout(120.0),
    )
    _raise_from_response(resp)


def send_system_note(
    agent_id: int,
    *,
    content: str,
    note_tag: str,
    source: str,
    task_id: int | None,
    resurrect: bool,
) -> int:
    """POST /api/agents/{id}/system-note — deliver a framework system note.

    Returns the durable inbound id; renders as a system note (no sender
    prefix), waking a live agent, reviving a terminated one when `resurrect`
    is set (same 120 s per-call timeout as send_message).
    """
    import httpx

    body: dict[str, str | bool | int] = {
        "content": content,
        "note_tag": note_tag,
        "source": source,
        "resurrect": resurrect,
    }
    if task_id is not None:
        body["task_id"] = task_id
    resp = _post(f"/api/agents/{agent_id}/system-note", body, timeout=httpx.Timeout(120.0))
    _raise_from_response(resp)
    return int(resp.json()["inbound_id"])


def get_last_message(agent_id: int, caller: str) -> str | None:
    """GET /api/agents/{id}/last-message → text of the last AI message."""
    resp = _get(f"/api/agents/{agent_id}/last-message", params={"caller": caller})
    _raise_from_response(resp)
    return resp.json()["text"]


def get_neighbors(agent_id: int, *, depth: int, limit: int) -> list[dict]:
    """GET /api/agents/{id}/neighbors → list of neighbor dicts.

    Each dict has id / label / status / depth / score, strongest first
    (order preserved from the gateway).
    """
    resp = _get(f"/api/agents/{agent_id}/neighbors", params={"depth": depth, "limit": limit})
    _raise_from_response(resp)
    return resp.json()["neighbors"]


def get_ancestors(agent_id: int) -> list[dict]:
    """GET /api/agents/{id}/neighbors → the `ancestors` rows: the spawn/fork
    chain above `agent_id`, nearest ancestor first (the gateway walks to the
    top, so the neighbors `depth`/`limit` params do not apply). Same dict
    shape as get_neighbors."""
    resp = _get(f"/api/agents/{agent_id}/neighbors", params={"depth": 1, "limit": 20})
    _raise_from_response(resp)
    return resp.json()["ancestors"]


def list_agents(filter_by_status: tuple[AgentStatus, ...] | None = None) -> list[dict[str, Any]]:
    """GET /api/agents → AgentSummary roster rows (optional status filter).

    The request sets ``fields=summary``. Each dict carries agent_id, spawner,
    fork_source_agent_id, status, pid, spawned_at, started_at, last_active_at,
    last_inbound_at, label, machine, supports_vision, liveness_state,
    notices_awaiting_response, unread_notice_count, and
    heartbeat_paused_until. The gateway first applies the broadest safe SQL
    scope: filters that cannot match terminated rows request ``scope=live``; a
    terminated-only filter requests ``scope=terminated``; mixed / unfiltered
    calls preserve the full historical ``scope=all`` contract. The exact
    public-status filter remains client-side.

    ``filter_by_status``: a non-empty tuple of AgentStatus values to keep;
    None or an empty tuple returns all agents unfiltered.
    """
    scope = "all"
    if filter_by_status:
        requested = set(filter_by_status)
        if requested == {AgentStatus.TERMINATED}:
            scope = "terminated"
        elif AgentStatus.TERMINATED not in requested:
            scope = "live"
    resp = _get("/api/agents", params={"scope": scope, "fields": "summary"})
    _raise_from_response(resp)
    rows: list[dict[str, Any]] = resp.json()
    if filter_by_status:
        rows = [r for r in rows if r["status"] in filter_by_status]
    return rows


def terminate(
    agent_id: int,
    *,
    source: str | None = None,
    message: str | None = None,
    force: bool = False,
) -> str:
    """POST /api/agents/{id}/terminate → status string.

    source defaults to f"agent:{ava.self.AGENT_ID}" so the lifecycle marker
    tells the peer who terminated it. Pass source=None to use the gateway
    default ("user").

    force=True requests interruption. Hosted force returns "enqueued" while
    the original host drains actual work; acceptance is not observed exit.
    Detached process force retains its "force_killed" result.

    message, when present, is retained for the agent's next resurrection while
    termination proceeds without waiting for another response.
    """
    body: dict = {}
    if source is not None:
        body["source"] = source
    else:
        body["source"] = ava._boot.default_actor()
    if message is not None:
        body["message"] = message
    if force:
        body["force"] = True
    resp = _post(f"/api/agents/{agent_id}/terminate", body)
    _raise_from_response(resp)
    return resp.json()["status"]


def exited(agent_id: int) -> None:
    """POST /api/agents/{id}/exited — report this process has reached its exit.

    Called from the process-exit path itself (not by a peer): the gateway
    finalizes status to 'terminated' (guarded, so a concurrent restart's
    'restarting' is left untouched), closes this agent's show() pages, and
    keeps daemon-supervised serve() pages open. The body carries the original
    admitted process incarnation. Legacy hosted callers send no body and may
    finalize only rows with unknown ownership, never a token-owned process.
    """
    from shared.runtime_incarnation import current_incarnation

    incarnation = current_incarnation(agent_id)
    body = (
        {"generation": str(incarnation.generation), "owner": str(incarnation.owner)}
        if incarnation is not None
        else None
    )
    resp = _post(f"/api/agents/{agent_id}/exited", body)
    _raise_from_response(resp)


def restart(agent_id: int, *, source: str | None = None) -> str:
    """POST /api/agents/{id}/restart → status string.

    source defaults to f"agent:{ava.self.AGENT_ID}" so the lifecycle marker
    tells the peer who restarted it. Pass source=None to use the gateway
    default ("user").
    """
    body: dict = {}
    if source is not None:
        body["source"] = source
    else:
        body["source"] = ava._boot.default_actor()
    resp = _post(f"/api/agents/{agent_id}/restart", body)
    _raise_from_response(resp)
    return resp.json()["status"]


def resurrect(agent_id: int, *, prompt: str, resurrected_by: str | None = None) -> str:
    """POST /api/agents/{id}/resurrect -> status string.

    resurrected_by defaults to f"agent:{ava.self.AGENT_ID}" so the
    lifecycle marker tells the peer who resurrected it. Pass
    resurrected_by=None to use the gateway default ("user").

    prompt is required -- a resurrected agent needs to know why it
    was woken up and what to do.
    """
    body: dict = {"prompt": prompt}
    if resurrected_by is not None:
        body["resurrected_by"] = resurrected_by
    else:
        body["resurrected_by"] = ava._boot.default_actor()
    resp = _post(f"/api/agents/{agent_id}/resurrect", body)
    _raise_from_response(resp)
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
    """POST /api/agents/{id}/pages → PageRow dict (id/name/port/title/...).

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
    resp = _post(f"/api/agents/{agent_id}/pages", body)
    _raise_from_response(resp)
    return resp.json()


def close_page(agent_id: int, name: str) -> None:
    """DELETE /api/agents/{id}/pages/{name}. 404 → httpx.HTTPStatusError."""
    resp = _delete(f"/api/agents/{agent_id}/pages/{name}")
    _raise_from_response(resp)


def list_open_pages(agent_id: int) -> list[dict]:
    """GET /api/agents/{id}/pages → the agent's open PageRow dicts."""
    resp = _get(f"/api/agents/{agent_id}/pages")
    _raise_from_response(resp)
    return list(resp.json())


def list_machines() -> list[dict]:
    """GET /api/cluster/machines → list of {name, description, live} dicts."""
    resp = _get("/api/cluster/machines")
    _raise_from_response(resp)
    return list(resp.json())


def list_presets() -> list[dict]:
    """GET /api/presets → list of preset dicts, ordered by name."""
    resp = _get("/api/presets")
    _raise_from_response(resp)
    return list(resp.json())


def get_preset(name: str) -> dict:
    """Look up a single preset by its unique name.

    Fetches the full list and filters client-side — efficient for the
    expected ≤10 presets; avoids a dedicated GET-by-name gateway endpoint.
    Raises ``PresetNotFoundError`` when no preset matches ``name``.
    """
    presets = list_presets()
    for p in presets:
        if p["name"] == name:
            return p
    raise PresetNotFoundError(f"preset {name!r} not found")


class PresetNotFoundError(Exception):
    """No preset with that name exists."""
