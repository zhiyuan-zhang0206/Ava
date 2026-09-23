"""Note-text chunking for the embedding index.

Split out of `services/memory_indexer/daemon.py` (2026-09-20, task #4222) when
the daemon's hard-exit migration pushed that file against its 800-line budget;
the daemon re-exports the private names for its existing test surface.

A long note's single embedding dilutes the entities mentioned in it (queries
like "hand off to 402" missed notes whose body carried the id). The body is
split at paragraph boundaries into blocks of ~1800 chars (~512 tokens),
overlapping by ~200 chars (~64 tokens) so a query spanning a boundary still
finds the note; the frontmatter description is embedded as its own row on top
of that.
"""

from __future__ import annotations

import re

from services.memory_indexer.backends.base import KIND_BODY, KIND_DESC
from shared.docs.notes import parse_note

_CHUNK_MAX_CHARS = 1800
_CHUNK_OVERLAP_CHARS = 200


def _split_note(content: str) -> tuple[str | None, str]:
    """Split one markdown file into (description, body).

    description is the frontmatter `description` (None when the file has no
    frontmatter or an empty one); body is the text after the frontmatter.
    Reuses the shared note parser so the desc vector and the description the
    search endpoint surfaces always agree.
    """
    note = parse_note(content, "memory.md")
    if note is None:
        return None, content
    description = note.description.strip() if note.description else None
    return description or None, note.body


def _chunk_body(
    body: str,
    *,
    max_chars: int = _CHUNK_MAX_CHARS,
    overlap_chars: int = _CHUNK_OVERLAP_CHARS,
) -> list[str]:
    """Split body text into overlapping chunks, preferring paragraph boundaries.

    Paragraphs (blank-line separated) pack greedily into chunks of at most
    `max_chars`; when the next paragraph would overflow, the chunk closes and
    the next one re-opens with the trailing paragraphs that fit in
    `overlap_chars`, so a query spanning a boundary still finds the note. A
    single paragraph longer than `max_chars` is hard-split by character with
    the same overlap. Returns [] for an empty body.
    """
    body = body.strip()
    if not body:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n[ \t]*\n", body)]
    paragraphs = [p for p in paragraphs if p]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def _flush() -> None:
        """Close the current chunk; carry its trailing paragraphs (up to
        `overlap_chars`) into the next chunk."""
        nonlocal current, current_len
        chunks.append("\n\n".join(current))
        tail: list[str] = []
        tail_len = 0
        for para in reversed(current):
            if tail_len + len(para) + (2 if tail_len else 0) > overlap_chars:
                break
            tail.insert(0, para)
            tail_len += len(para) + (2 if tail_len > 0 else 0)
        current = tail
        current_len = tail_len

    for para in paragraphs:
        if len(para) > max_chars:
            if current:
                chunks.append("\n\n".join(current))
                current, current_len = [], 0
            start = 0
            while start < len(para):
                end = min(start + max_chars, len(para))
                chunks.append(para[start:end])
                if end == len(para):
                    break
                start = end - overlap_chars
            continue
        if current and current_len + 2 + len(para) > max_chars:
            _flush()
        current.append(para)
        current_len += len(para) + (2 if current_len else 0)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _file_rows(content: str) -> list[tuple[str, int, str]]:
    """The chunk rows of one file: (kind, chunk_idx, text).

    The frontmatter `description` becomes one KIND_DESC row when present; the
    body splits into KIND_BODY chunks. A file with neither produces no rows
    (it is not searchable content; the cold-start reconcile tolerates that).
    """
    description, body = _split_note(content)
    rows: list[tuple[str, int, str]] = []
    if description:
        rows.append((KIND_DESC, 0, description))
    for i, chunk in enumerate(_chunk_body(body)):
        rows.append((KIND_BODY, i, chunk))
    return rows
