"""Second stage of memory search: a decision model ranks the chunks vector search found.

Vector search is a wide, cheap first pass: it returns each file's best-matching chunk, and
proximity in embedding space is only a proxy for "is worth reading". This stage restores the
text of every candidate chunk from the note file (with the same split rules the indexer used)
and asks a decision model, once per candidate, a yes/no question chosen by the search's purpose:
whether that note is worth the agent reading (`recall`, `search`), or whether it records
essentially the same knowledge as a note the agent just wrote (`dedup`). Results are ordered by
the probability of "yes". No text is generated anywhere on this path.

A search ranks at most `limit` candidates (the purpose's configured candidate count), however many
results the caller asked for: the cost of one search is fixed by configuration, not by its
arguments. Excluded notes are dropped before they are read or ranked, and do not take a slot.

One request per candidate, each with its own state (the query plus that one note) and a single
yes/no question, is the shape the score threshold of passive recall was calibrated on; the
wording below is part of that calibration, so changing the model, a question or a state template
means re-running the benchmark. Requests run concurrently up to `_MAX_CONCURRENT_DECISIONS`;
cancelling the search cancels the requests still running.

A failed decision call raises `IndexerUnavailable` with the cause: there is no fallback to vector
order, because a silent downgrade would hide a broken ranking behind plausible-looking results.
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from loguru import logger

from base.api_contracts.agent_contract import IndexerUnavailable
from base.api_contracts.memory_search import MemorySearchPurpose
from base.lm.catalog import ModelCatalog
from base.lm.catalog.decide.types import TRUE, DecisionError, YesNo
from base.lm.decision.call import decide
from base.packages.docs.notes import parse_note
from services.derived.memory_indexer.backends.base import ChunkHit
from services.derived.memory_indexer.chunking import chunk_text

USAGE_KINDS: dict[MemorySearchPurpose, str] = {
    "recall": "memory_rerank",
    "search": "memory_rerank",
    "dedup": "memory_write_dedup",
}
"""`llm_usage.usage_kind` of the decision calls per purpose, so rerank spend is separable from
chat spend and the duplicate check of written notes is separable from search."""

_MAX_CONCURRENT_DECISIONS = 30
"""Decision requests in flight at once for one search. At the default of 30 candidates every
request goes out together, which kept the ranking near 2s in the calibration benchmark; the
cap only bites when `memory_rerank_candidates` is raised."""

_EXCERPT_CHARS = 1800
"""The most characters of a chunk shown to the model (the indexer's chunk size)."""

_QUESTIONS: dict[MemorySearchPurpose, YesNo] = {
    "recall": YesNo(
        question=(
            "The memory note excerpt contains something the agent should read right now to handle "
            "the latest messages well: a rule, ruling, procedure, prior decision, or fact its "
            "current work depends on. Merely sharing a topic or keywords with the conversation "
            "is not enough."
        ),
        true_if="The note is worth the agent reading now.",
        false_if="The note is only loosely related, or unrelated, to what the agent needs now.",
    ),
    "search": YesNo(
        question=(
            "The memory note excerpt helps with what the agent is searching for, given the "
            "agent's recent context: it contains the information, rule, procedure or precedent "
            "the query is looking for. Merely sharing a topic or keywords with the query is not "
            "enough."
        ),
        true_if="The note helps answer the search query in this situation.",
        false_if="The note does not help answer the search query.",
    ),
    "dedup": YesNo(
        question=(
            "The existing memory note excerpt records essentially the same knowledge as the new "
            "note: the same rule, fact, ruling or procedure, so that the new note should be "
            "merged into the existing one (or replace it) instead of being kept as a second "
            "note. Sharing a topic or keywords is not enough: a note that adds a different rule, "
            "a different scope, or a different decision about the same subject is not the same "
            "knowledge."
        ),
        true_if="The new note duplicates what the existing note already records.",
        false_if="The new note records knowledge the existing note does not.",
    ),
}
"""The question per purpose. `recall` and `search` are variant A of the calibration benchmark;
`dedup` is not calibrated (its threshold is `memory_write_dedup_threshold`)."""

_STATE_TEMPLATES: dict[MemorySearchPurpose, tuple[str, str]] = {
    "recall": (
        "Recent conversation of an AI agent (most recent last):",
        "Candidate memory note:",
    ),
    "search": (
        "Search query the agent issued against its memory notes:",
        "Candidate memory note:",
    ),
    "dedup": (
        "New memory note an AI agent has just written:",
        "Existing memory note:",
    ),
}
"""Per purpose, the lead-in of the query and the lead-in of the candidate note in the state."""


class RankedChunk(NamedTuple):
    """One candidate after ranking.

    path: note path relative to the memory pool root.
    score: probability (0..1) that the chunk is relevant to the query.
    chunk: the chunk's text, as the indexer split it.
    description: the note's frontmatter description, empty when it has none.
    tags: the note's frontmatter tags.
    """

    path: str
    score: float
    chunk: str
    description: str
    tags: list[str]


@dataclass(frozen=True)
class _Candidate:
    path: str
    chunk: str
    description: str
    tags: list[str]


# ── the decision-model contract (the only code that touches base.lm.decision.call) ──


async def ask_relevance(
    model: str, state: str, question: YesNo, *, catalog: ModelCatalog, usage_kind: str
) -> float:
    """Probability of "yes" to `question` about `state`, from one decision call."""
    try:
        decisions = await decide(
            model,
            state,
            {"relevant": question},
            catalog=catalog,
            usage_kind=usage_kind,
            source="memory_search",
        )
    except DecisionError as exc:
        logger.warning(
            "memory rerank failed with decision model {model}: {err}", model=model, err=repr(exc)
        )
        raise IndexerUnavailable(f"memory rerank failed ({model}): {exc}") from exc
    # A probability is within [0, 1] up to the provider's rounding; clamp so the response
    # contract (0..1) holds exactly.
    return min(1.0, max(0.0, decisions["relevant"].probability(TRUE)))


# ── candidates ──


def _pool_relative(path: str, root: Path) -> tuple[Path, str]:
    """The resolved file of an index path and its path relative to the pool root.

    An index path outside the pool keeps the path as stored.
    """
    file = Path(path).resolve()
    try:
        return file, str(file.relative_to(root))
    except ValueError:
        return file, path


def _load_candidates(
    hits: Sequence[ChunkHit], memory_root: Path, exclude: Collection[str], limit: int
) -> list[_Candidate]:
    """Restore the chunk text of the first `limit` hits that are not excluded.

    An excluded hit is passed over before its file is read. A hit whose file is gone,
    unreadable, or no longer produces that chunk is skipped too: the index lags the pool by
    seconds to minutes, and a row the file no longer has cannot be judged. Neither takes one of
    the `limit` slots, so the next-closest hit fills it.
    """
    root = memory_root.resolve()
    candidates: list[_Candidate] = []
    for hit in hits:
        if len(candidates) == limit:
            break
        file, rel = _pool_relative(hit.path, root)
        if rel in exclude:
            continue
        try:
            content = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        chunk = chunk_text(content, hit.kind, hit.chunk_idx)
        if chunk is None:
            continue
        note = parse_note(content, file.name)
        description = note.description.strip() if note and note.description else ""
        tags = list(note.tags) if note else []
        candidates.append(_Candidate(rel, chunk, description, tags))
    return candidates


def _pair_state(query: str, candidate: _Candidate, purpose: MemorySearchPurpose) -> str:
    """The state shown to the decision model for one (query, candidate chunk) pair."""
    query_header, note_header = _STATE_TEMPLATES[purpose]
    note = (
        f"path: {candidate.path}\n"
        f"description: {candidate.description or '(none)'}\n"
        f"excerpt:\n{candidate.chunk[:_EXCERPT_CHARS]}"
    )
    return f"{query_header}\n{query}\n\n{note_header}\n{note}"


# ── entry point ──


async def _score_all(
    model: str,
    states: list[str],
    question: YesNo,
    catalog: ModelCatalog,
    usage_kind: str,
) -> list[float]:
    """One request per state, at most `_MAX_CONCURRENT_DECISIONS` at once; the first failure
    cancels the rest and is raised as itself."""
    permits = asyncio.Semaphore(_MAX_CONCURRENT_DECISIONS)

    async def score(state: str) -> float:
        async with permits:
            return await ask_relevance(
                model, state, question, catalog=catalog, usage_kind=usage_kind
            )

    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(score(state)) for state in states]
    except ExceptionGroup as failures:
        raise failures.exceptions[0] from None
    return [task.result() for task in tasks]


async def rerank(
    query: str,
    hits: Sequence[ChunkHit],
    *,
    purpose: MemorySearchPurpose,
    model: str,
    catalog: ModelCatalog,
    k: int,
    limit: int,
    exclude: Collection[str],
    memory_root: Path,
) -> list[RankedChunk]:
    """Up to `k` of the `limit` candidates most worth reading for `query`, best first.

    `hits` come from vector search (one best chunk per file), closest first; the first `limit`
    of them that are not in `exclude` (pool-relative paths) are ranked, one decision request
    each, so `k` above `limit` returns `limit` results. Raises `IndexerUnavailable` when the
    decision model cannot answer; an empty `hits`, or hits whose files are all gone or excluded,
    give [].
    """
    candidates = await asyncio.to_thread(_load_candidates, hits, memory_root, exclude, limit)
    if not candidates:
        return []
    states = [_pair_state(query, candidate, purpose) for candidate in candidates]
    scores = await _score_all(model, states, _QUESTIONS[purpose], catalog, USAGE_KINDS[purpose])
    ranked = sorted(range(len(candidates)), key=lambda i: scores[i], reverse=True)[:k]
    return [
        RankedChunk(
            path=candidates[i].path,
            score=scores[i],
            chunk=candidates[i].chunk,
            description=candidates[i].description,
            tags=candidates[i].tags,
        )
        for i in ranked
    ]
