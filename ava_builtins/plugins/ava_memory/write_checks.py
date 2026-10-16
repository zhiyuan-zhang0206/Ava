from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from base.agents.context import AvaContext
from base.agents.context.gateway_client import memory_search as memory_client
from base.api_contracts.agent_contract import GatewayUnavailable, IndexerUnavailable
from base.host.env.agent_slices import AgentSlices
from base.log import logger

from .template import validate

# ava:ref ava_builtins/plugins/ava_memory/docs/memory-write-check.ava.okf.md#write-check-duplicates
_SEARCH_K = 5

# ava:ref ava_builtins/plugins/ava_memory/docs/memory-write-check.ava.okf.md#write-check-duplicates
_QUERY_CHAR_CAP = 4000

_LOG_LABEL = "memory-write-check"


# ava:ref ava_builtins/plugins/ava_memory/docs/memory-write-check.ava.okf.md#write-check-duplicates
@dataclass(frozen=True)
class Duplicate:
    path: Path
    score: float
    description: str


# ava:ref ava_builtins/plugins/ava_memory/docs/memory-write-check.ava.okf.md#write-check-rules
@dataclass(frozen=True)
class NoteReport:
    path: Path
    rule_errors: tuple[str, ...]
    duplicates: tuple[Duplicate, ...]

    @property
    def has_findings(self) -> bool:
        return bool(self.rule_errors or self.duplicates)


# ava:ref ava_builtins/plugins/ava_memory/docs/memory-write-check.ava.okf.md#write-check-duplicates
def _query_of(path: Path) -> str | None:
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


# ava:ref ava_builtins/plugins/ava_memory/docs/memory-write-check.ava.okf.md#write-check-degrade
async def _search(
    query: str, *, deadline: float, context: AvaContext
) -> list[memory_client.MemorySearchResult] | None:
    import httpx

    try:
        return await asyncio.to_thread(
            memory_client.memory_search,
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
        logger.error(
            "[{}] duplicate search failed with HTTP {} at {}",
            _LOG_LABEL,
            exc.response.status_code,
            exc.request.url,
        )
        return None


# ava:ref ava_builtins/plugins/ava_memory/docs/memory-write-check.ava.okf.md#write-check-duplicates
async def _duplicates_of(
    pool: Path, relative: str, *, threshold: float, deadline: float, context: AvaContext
) -> tuple[Duplicate, ...]:
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
    errors = await asyncio.to_thread(validate.validate_note, root / relative)
    duplicates = (
        await _duplicates_of(
            root, relative, threshold=threshold, deadline=deadline, context=context
        )
        if shared
        else ()
    )
    return NoteReport(root / relative, tuple(errors), duplicates)


# ava:ref ava_builtins/plugins/ava_memory/docs/memory-write-check.ava.okf.md#write-check-rules
async def check_written_notes(
    shared: set[str],
    personal: set[str],
    *,
    pool: Path,
    personal_root: Path,
    agent: AgentSlices,
    context: AvaContext,
) -> list[NoteReport]:
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


# ava:ref ava_builtins/plugins/ava_memory/docs/memory-write-check.ava.okf.md#write-check-rules
def render_reminder(reports: list[NoteReport]) -> str:
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
