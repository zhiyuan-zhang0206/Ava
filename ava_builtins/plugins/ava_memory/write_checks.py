"""Check the memory notes an exec call wrote — the agent-process half of the write check.

An agent writes memory notes with `ava.files.write` / `append`; the wraps in `plugin.py` record
which ones. After the call, `agent_runtime.py` hands that record here and the findings come back as
one reminder note. Two checks, and neither changes, blocks or rejects a write:

- Rules, for every written note (shared and personal): the pool's own single-note rules, imported
  from `template/validate.py` where they live (frontmatter fields, exactly one valid `type/<x>`
  tag, the character cap). Index, directory and pointer rules need the whole pool and are the
  steward's.
- Duplicates, for shared notes only: the note is searched against the pool with purpose `dedup`,
  and the gateway's decision model answers, for each of the closest notes, whether it records
  essentially the same knowledge. Candidates at or above `memory_write_dedup_threshold` are named.
  The threshold is not benchmark-calibrated, so every probability is logged.

A search that cannot be answered (index or ranking model down, gateway erroring) leaves that note
without duplicate findings, like passive recall skipping a turn. This module sends the search
through `ava.gateway_client`; it imports no decision-model code.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from ava import gateway_client
from base.agents.context import AvaContext
from base.api_contracts.agent_contract import GatewayUnavailable, IndexerUnavailable
from base.host.env.agent_slices import AgentSlices
from base.log import logger

from .template import validate

_SEARCH_K = 5
"""Closest notes whose duplicate probability is considered for one written note. The search
ranks exactly that many (`memory_dedup_candidates`), so when the note itself is already indexed
(an update of an existing note) it takes one of the slots and four others are judged."""

_QUERY_CHAR_CAP = 4000
"""Most characters of the written note sent as the search query (description, then body)."""

_LOG_LABEL = "memory-write-check"


@dataclass(frozen=True)
class Duplicate:
    """An existing shared note the decision model judged to record the same knowledge."""

    path: Path
    score: float
    description: str


@dataclass(frozen=True)
class NoteReport:
    """What the checks found about one written note."""

    path: Path
    rule_errors: tuple[str, ...]
    duplicates: tuple[Duplicate, ...]

    @property
    def has_findings(self) -> bool:
        return bool(self.rule_errors or self.duplicates)


def _query_of(path: Path) -> str | None:
    """The text a note is searched by: its description, then its body, capped; the whole text
    when its frontmatter cannot be read (the rules check names that). None when there is none."""
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    parts = validate.note_parts(content)
    if parts is None:
        query = content.strip()
    else:
        frontmatter, body = parts
        description = frontmatter.get("description")
        head = description.strip() if isinstance(description, str) else ""
        query = f"{head}\n\n{body.strip()}".strip()
    return query[:_QUERY_CHAR_CAP] or None


async def _search(
    query: str, *, deadline: float, context: AvaContext
) -> list[gateway_client.MemorySearchResult] | None:
    """The `dedup` search results, or None when the search is unavailable or failed."""
    import httpx  # deferred: stays off the child boot path

    try:
        return await asyncio.to_thread(
            gateway_client.memory_search,
            query,
            _SEARCH_K,
            purpose="dedup",
            timeout=deadline,
            context=context,
        )
    except (GatewayUnavailable, IndexerUnavailable) as exc:
        logger.debug("[{}] duplicate search unavailable: {!r}", _LOG_LABEL, exc)
        return None
    except httpx.HTTPStatusError as exc:
        # An error status carrying no wire `reason`: the gateway failed in a way it does not
        # model. The check degrades like passive recall does, but someone has to see it.
        logger.error(
            "[{}] duplicate search failed with HTTP {} at {}",
            _LOG_LABEL,
            exc.response.status_code,
            exc.request.url,
        )
        return None


async def _duplicates_of(
    pool: Path, relative: str, *, threshold: float, deadline: float, context: AvaContext
) -> tuple[Duplicate, ...]:
    """Existing shared notes at or above `threshold` as duplicates of the note at `relative`."""
    query = await asyncio.to_thread(_query_of, pool / relative)
    if query is None:
        return ()
    results = await _search(query, deadline=deadline, context=context)
    if results is None:
        return ()
    others = [item for item in results if item.path != relative][:_SEARCH_K]
    logger.info(
        "[{}] {}: duplicate probabilities {}",
        _LOG_LABEL,
        relative,
        ", ".join(f"{item.path}={item.score:.2f}" for item in others) or "(no candidates)",
    )
    return tuple(
        Duplicate(pool / item.path, item.score, item.description)
        for item in others
        if item.score >= threshold
    )


async def _check_one(
    root: Path,
    relative: str,
    *,
    shared: bool,
    threshold: float,
    deadline: float,
    context: AvaContext,
) -> NoteReport:
    """Rules for the note at `root / relative`, plus duplicates when it is a shared note."""
    errors = await asyncio.to_thread(validate.validate_note, root / relative)
    duplicates = (
        await _duplicates_of(
            root, relative, threshold=threshold, deadline=deadline, context=context
        )
        if shared
        else ()
    )
    return NoteReport(root / relative, tuple(errors), duplicates)


async def check_written_notes(
    shared: set[str],
    personal: set[str],
    *,
    pool: Path,
    personal_root: Path,
    agent: AgentSlices,
    context: AvaContext,
) -> list[NoteReport]:
    """Check the written notes (paths relative to `pool` / `personal_root`); the reports of those
    with a finding, shared notes first, each group in path order.

    A note that is gone, or is not a note (an index file), is skipped."""
    threshold = agent.memory.memory_write_dedup_threshold
    deadline = agent.memory.memory_write_check_deadline_seconds
    reports = await asyncio.gather(
        *(
            _check_one(
                root,
                relative,
                shared=is_shared,
                threshold=threshold,
                deadline=deadline,
                context=context,
            )
            for root, names, is_shared in ((pool, shared, True), (personal_root, personal, False))
            for relative in sorted(names)
            if validate.is_note_name(Path(relative).name) and (root / relative).is_file()
        )
    )
    return [report for report in reports if report.has_findings]


def render_reminder(reports: list[NoteReport]) -> str:
    """The reminder the agent reads: per note, the rules it breaks and the notes it may duplicate."""
    lines = ["Memory write check on the notes written by the call above:"]
    for report in reports:
        lines.append(f"\n{report.path}")
        lines.extend(f"  - {error}" for error in report.rule_errors)
        for duplicate in report.duplicates:
            about = f"; it holds: {duplicate.description}" if duplicate.description else ""
            lines.append(
                f"  - Possibly records the same knowledge as {duplicate.path} "
                f"(probability {duplicate.score:.2f}{about}). If it does, update that note "
                "with what is new and remove this one instead of keeping both."
            )
    lines.append(
        "\nEdit a note with ava.files.edit to fix a rule. Nothing was changed or rejected; "
        "this is only a reminder."
    )
    return "\n".join(lines)
