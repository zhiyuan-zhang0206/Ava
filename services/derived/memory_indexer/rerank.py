"""Second stage of memory search: a decision model ranks the chunks vector search found.

Vector search is a wide, cheap first pass: it returns each file's best-matching chunk, and
proximity in embedding space is only a proxy for "answers the query". This stage restores the
text of every candidate chunk from the note file (with the same split rules the indexer used),
asks a decision model one yes/no question per chunk — "is this excerpt relevant to the query?" —
and ranks by the probability of "yes". No text is generated anywhere on this path.

A decision call has a context limit (the state plus the longest question, and the state plus all
questions), and the provider never truncates, so this module batches the questions itself. A
failed decision call raises `IndexerUnavailable` with the cause: there is no fallback to vector
order, because a silent downgrade would hide a broken ranking behind plausible-looking results.

The ranker's contract with `base.lm.catalog.decide` is confined to `decision_limits` and
`ask_relevance`; everything else is plain ordering and batching.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from loguru import logger

from base.agents import IndexerUnavailable
from base.lm.catalog import ModelCatalog
from base.lm.catalog.decide.call import decide
from base.lm.catalog.decide.types import (
    TRUE,
    DecisionError,
    YesNo,
    estimate_tokens,
    question_text,
)
from base.packages.docs.notes import parse_note
from services.derived.memory_indexer.backends.base import ChunkHit
from services.derived.memory_indexer.chunking import chunk_text

USAGE_KIND = "memory_rerank"
"""`llm_usage.usage_kind` of the decision calls, so rerank spend is separable from chat spend."""

_TRUE_IF = "The excerpt contains information that bears on the text being analyzed."
_FALSE_IF = (
    "The excerpt is unrelated to the text being analyzed, or only shares words or a broad "
    "topic with it."
)


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
class DecisionLimits:
    """The decision model's input limits in estimated tokens."""

    context_window: int
    request_limit: int


@dataclass(frozen=True)
class _Candidate:
    path: str
    chunk: str
    description: str
    tags: list[str]


# ── the decision-model contract (the only code that touches base.lm.catalog.decide.call) ──


def decision_limits(model: str, catalog: ModelCatalog) -> DecisionLimits:
    """The input limits of decision model `model`."""
    spec = catalog.decision_models.get(model)
    if spec is None:
        raise IndexerUnavailable(
            f"memory rerank model {model!r} is not a known decision model; "
            f"known: {', '.join(sorted(catalog.decision_models))}"
        )
    return DecisionLimits(context_window=spec.context_window, request_limit=spec.request_limit)


async def ask_relevance(
    model: str, query: str, questions: Mapping[str, YesNo], *, catalog: ModelCatalog
) -> dict[str, float]:
    """Probability of "yes" for every question, from one decision call about `query`."""
    try:
        decisions = await decide(
            model, query, questions, catalog=catalog, usage_kind=USAGE_KIND, source="memory_search"
        )
    except DecisionError as exc:
        logger.warning(
            "memory rerank failed with decision model {model}: {err}", model=model, err=repr(exc)
        )
        raise IndexerUnavailable(f"memory rerank failed ({model}): {exc}") from exc
    # A probability is within [0, 1] up to the provider's rounding; clamp so the response
    # contract (0..1) holds exactly.
    return {
        key: min(1.0, max(0.0, decision.probability(TRUE))) for key, decision in decisions.items()
    }


# ── candidates ──


def _load_candidates(hits: Sequence[ChunkHit], memory_root: Path) -> list[_Candidate]:
    """Restore each hit's chunk text from its note file.

    A hit whose file is gone, unreadable, or no longer produces that chunk is skipped: the
    index lags the pool by seconds to minutes, and a row the file no longer has cannot be judged.
    """
    root = memory_root.resolve()
    candidates: list[_Candidate] = []
    for hit in hits:
        file = Path(hit.path).resolve()
        try:
            rel = str(file.relative_to(root))
        except ValueError:
            rel = hit.path
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


def _question_of(candidate: _Candidate) -> YesNo:
    description = f"Description: {candidate.description}\n" if candidate.description else ""
    return YesNo(
        question=(
            "Is the memory note excerpt below relevant to the text being analyzed?\n\n"
            f"Note: {candidate.path}\n{description}\nExcerpt:\n{candidate.chunk}"
        ),
        true_if=_TRUE_IF,
        false_if=_FALSE_IF,
    )


# ── batching ──


def _batches(
    questions: Mapping[str, YesNo], query_tokens: int, limits: DecisionLimits
) -> list[dict[str, YesNo]]:
    """Split `questions` into groups that each fit one decision request.

    A group's questions plus the query stay within `request_limit`; a single question that is
    too large on its own gets its own group and is rejected by the decision call with a clear
    error, rather than being silently shortened here.
    """
    budget = limits.request_limit - query_tokens
    batches: list[dict[str, YesNo]] = []
    current: dict[str, YesNo] = {}
    used = 0
    for key, question in questions.items():
        tokens = estimate_tokens(question_text(question))
        if current and used + tokens > budget:
            batches.append(current)
            current, used = {}, 0
        current[key] = question
        used += tokens
    if current:
        batches.append(current)
    return batches


async def _ask_all(
    model: str, query: str, batches: list[dict[str, YesNo]], catalog: ModelCatalog
) -> dict[str, float]:
    """Run every batch concurrently; the first failure cancels the rest and is raised as itself."""
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [
                group.create_task(ask_relevance(model, query, batch, catalog=catalog))
                for batch in batches
            ]
    except ExceptionGroup as failures:
        raise failures.exceptions[0] from None
    return {key: p for task in tasks for key, p in task.result().items()}


# ── entry point ──


async def rerank(
    query: str,
    hits: Sequence[ChunkHit],
    *,
    model: str,
    catalog: ModelCatalog,
    k: int,
    memory_root: Path,
) -> list[RankedChunk]:
    """The `k` candidates most likely relevant to `query`, most relevant first.

    `hits` come from vector search (one best chunk per file). Raises `IndexerUnavailable` when
    the decision model cannot answer; an empty `hits`, or hits whose files are all gone, give [].
    """
    candidates = await asyncio.to_thread(_load_candidates, hits, memory_root)
    if not candidates:
        return []
    questions = {str(i): _question_of(c) for i, c in enumerate(candidates)}
    batches = _batches(questions, estimate_tokens(query), decision_limits(model, catalog))
    probabilities = await _ask_all(model, query, batches, catalog)
    ranked = sorted(range(len(candidates)), key=lambda i: probabilities[str(i)], reverse=True)[:k]
    return [
        RankedChunk(
            path=candidates[i].path,
            score=probabilities[str(i)],
            chunk=candidates[i].chunk,
            description=candidates[i].description,
            tags=candidates[i].tags,
        )
        for i in ranked
    ]
