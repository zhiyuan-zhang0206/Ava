"""Passive memory recall: content-triggered injection of memory-pool notes.

Where `memory_index_note` (this plugin's `notes.py`) keeps the standing index
(MEMORY.md) permanently in front of the agent, passive recall reaches into the
*rest* of the pool: before a turn woken by new inbound, it runs a semantic
search keyed on the recent conversation and injects the top matches as a
system-styled note. Each match carries the same fields `ava.memory.search`
exposes -- the pool-relative path and the note's frontmatter description
(empty when the note has none; never synthesized from title/body). The agent
sees relevant durable notes surface on their own, without having to call
`ava.memory.search`.

A strippable layer: gated by `settings.agent.passive_memory_recall_enabled` (default
off), and a no-op whenever the search does not come back with results -- index
down, gateway erroring, feature off -- so the call site degrades to nothing
rather than failing the turn. It runs before the LLM on every inbound turn, so a
search failure that escaped would kill the agent process, not just the recall.
Same-session dedup is the caller's job (it passes the already-injected paths in
and persists the ones this call added).
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any, NamedTuple, cast

import httpx
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from agent.graph._memory_filter import Candidate, filter_candidates
from agent.messages import NoteTag, system_note_message
from ava import _gateway_client
from shared.agents import GatewayUnavailable, IndexerUnavailable
from shared.config import settings
from shared.lm.content import content_blocks
from shared.log import logger
from shared.message_kwargs import AvaMsgType, read_ava_kwargs
from shared.paths import memory_dir

# How many recent conversation messages form the search query. How many notes are
# retrieved and how many are injected are two different numbers now
# (`memory_recall_retrieve_k` / `memory_recall_inject_k`): retrieval goes wide so
# the filter has candidates to reject, injection stays narrow because notes crowd
# the context they are meant to inform.
_QUERY_MESSAGES = 6
# Bound on the query text so a single huge paste does not become the query.
_QUERY_CHAR_CAP = 2000

# Agent-visible framing. Observational, no implementation detail (obeys the SDK
# docstring discipline since the agent reads this text).
_FRAMING = (
    "Memory notes related to the current conversation, retrieved "
    "automatically. These point into your note pool at ava.memory.PATH -- read "
    "a path in full when it looks relevant."
)


class PassiveRecall(NamedTuple):
    """Result of a passive recall pass.

    note: the system-styled human message to inject.
    paths: the memory-pool-relative note paths it lists -- the caller records
        these so the same note is not injected again this session.
    """

    note: HumanMessage
    paths: set[str]


def _message_text(msg: AnyMessage) -> str:
    """Best-effort plain text of a message's content (str or multimodal list)."""
    content: Any = msg.content  # pyright: ignore[reportUnknownMemberType]
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content_blocks(content):
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(
            text := cast(dict[str, Any], block).get("text"), str
        ):
            parts.append(text)
    return " ".join(parts)


def _build_query(messages: Collection[AnyMessage]) -> str:
    """Concatenate the last few genuine conversation messages into a query.

    Genuine = agent replies (AIMessage) and inbound chat (HumanMessage tagged
    `ava_msg_type='inbound'`). Framework system-notes (heartbeat, lifecycle
    markers, and prior recall notes themselves) are excluded so the query
    reflects the conversation, not our own injections -- otherwise recall would
    feed on its own output.
    """
    picked: list[str] = []
    for msg in reversed(list(messages)):
        if isinstance(msg, AIMessage) or (
            isinstance(msg, HumanMessage)
            and read_ava_kwargs(msg).get("ava_msg_type") == AvaMsgType.INBOUND
        ):
            text = _message_text(msg)
        else:
            continue
        text = text.strip()
        if text:
            picked.append(text)
        if len(picked) >= _QUERY_MESSAGES:
            break
    picked.reverse()
    return "\n".join(picked)[-_QUERY_CHAR_CAP:]


async def passive_memory_recall(
    messages: Collection[AnyMessage], *, injected_paths: Collection[str] = frozenset()
) -> PassiveRecall | None:
    """Search the memory pool on the recent conversation and render the fresh
    top matches as a note to inject, or `None` when there is nothing to add.

    `None` when: the feature is disabled, the conversation yields no query, the
    search failed (index unavailable, or the gateway answered with an error
    status), or every match is already in `injected_paths` (or not present on
    this machine yet). `injected_paths` and the returned `PassiveRecall.paths`
    are memory-pool-relative.

    Never raises on a failed search: the caller is a before_llm hook, so an
    exception here ends the agent process rather than the recall.
    """
    if not settings.agent.passive_memory_recall_enabled:
        return None
    query = _build_query(messages)
    if not query:
        return None
    retrieve_k = settings.agent.memory_recall_retrieve_k
    try:
        results = await asyncio.to_thread(_gateway_client.memory_search, query, retrieve_k)
    except (GatewayUnavailable, IndexerUnavailable) as exc:
        # Recall is an enhancement; a memory-index outage must not crash the
        # turn. Skip this turn and let the next one retry. Debug level because
        # the gateway said this in the wire contract -- a restart or a stalled
        # embedder is a modelled, self-clearing state, not news.
        logger.debug(
            "[{label}] {body}",
            label="passive-recall",
            body=f"memory search unavailable: {exc!r}",
            event="passive_recall",
        )
        return None
    except httpx.HTTPStatusError as exc:
        # The endpoint failed in a way it does not model: a status whose body
        # carries no wire `reason`, so `_raise_from_response` re-raises the raw
        # HTTP error to fail fast. Fail-fast is right for a call the agent made
        # on purpose, but recall runs before the LLM on every inbound turn, so
        # here it took the whole process down with it -- one intermittent 500
        # killed agent 405 on 2026-08-07. Degrade to no recall, at error level:
        # unlike the branch above, nobody designed this response, so it is a
        # gateway bug someone has to see.
        #
        # Only the status-code half of `httpx.HTTPError` is caught. Its
        # transport half never reaches here: `_gateway_client._post` retries
        # those and converts them to `GatewayUnavailable`, which the branch
        # above already takes.
        logger.error(
            "[{label}] {body}",
            label="passive-recall",
            body=(
                f"memory search failed with HTTP {exc.response.status_code} "
                f"at {exc.request.url}; continuing with no recall this turn"
            ),
            event="passive_recall",
        )
        return None

    # Cheap local reject: a file that has not reached this machine yet is
    # dropped before the filter is asked to judge it -- it cannot be injected
    # either way. Already-injected paths are NOT dropped here: the filter must
    # judge the full candidate set, or a second message close to the first
    # would have its best matches pre-removed and inject unrelated notes that
    # merely outranked the deduped ones. Dedup happens after the filter, on
    # what the filter judged relevant.
    root = memory_dir()
    candidates: list[Candidate] = []
    by_path: dict[str, str] = {}
    for item in results:
        rel = item.path
        if not (root / rel).is_file():
            # Search can return a path not yet synced to this machine; skip it
            # (it will be recallable once it arrives).
            continue
        candidates.append(Candidate(path=rel, description=item.description, tags=list(item.tags)))
        by_path[rel] = item.description

    if not candidates:
        return None

    picked = await filter_candidates(query, candidates)
    if not picked:
        # The filter judged none of them relevant. Injecting nothing is the
        # point: an unfiltered recall always had something to show.
        return None

    # Dedup now, on what the filter judged relevant: a note already injected
    # this session is dropped; if everything the filter picked is already in
    # front of the agent, there is nothing new to add.
    already = set(injected_paths)
    fresh = [rel for rel in picked if rel not in already]
    if not fresh:
        return None

    # Present exactly the fields ava.memory.search returns: path + the
    # frontmatter description (empty when the note has none -- not backfilled
    # from title/body, so the note stays a faithful mirror of what search would
    # surface).
    lines = [f"- {rel}: {by_path[rel]}" if by_path[rel] else f"- {rel}" for rel in fresh]
    fresh_set = set(fresh)
    note = system_note_message(
        content=f"{_FRAMING}\n\n" + "\n".join(lines),
        tag=NoteTag.MEMORY,
        created_at=datetime.now(UTC),
    )
    return PassiveRecall(note=note, paths=fresh_set)
