"""Shared markdown-note model + walkers — one parser for the whole repo.

The memory pool graph (`gateway/routers/memory.py`) and the OKF bundle graph
(`shared/okf_graph.py`) both build graphs out of `---`-frontmatter markdown
notes; this module is the single place that reads a note off disk, decides it
is a note, and maps frontmatter to the fields both consumers need. It stays
schema-free: `Note` is plain data, and graph-specific semantics (primary_tag,
edge filtering) live in the callers.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from shared.frontmatter import parse_frontmatter_typed

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


@dataclass(frozen=True)
class Note:
    """One parsed markdown note from a note pool.

    `rel` is the posix path relative to the pool root, sans extension — the
    stable node id both graph consumers key on. `fm` keeps the raw frontmatter
    dict for callers that read fields this dataclass does not lift.
    """

    rel: str
    title: str
    description: str | None
    tags: tuple[str, ...]
    timestamp: str | None
    ava_agent: str | None
    ava_machine: str | None
    body: str
    fm: dict[str, Any]


def normalize_tags(raw: object) -> tuple[str, ...]:
    """Coerce a frontmatter `tags` value to a tuple of strings.

    A bare string is wrapped; a list keeps only its string elements; anything
    else (`None`, an int, a dict) is an empty tuple — tags are a string list
    and nothing else, so a malformed note degrades to untagged instead of
    crashing the caller (`tags: 5` used to TypeError the graph endpoint).
    """
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, list):
        return tuple(t for t in cast("list[object]", raw) if isinstance(t, str))
    return ()


def parse_note(text: str, rel: str) -> Note | None:
    """Parse one note's frontmatter into a `Note`; `None` when the text is not
    a note (no/invalid frontmatter) — the caller skips it silently."""
    parsed = parse_frontmatter_typed(text)
    if parsed is None:
        return None
    fm, body = parsed

    title = fm.get("title", rel)
    if not isinstance(title, str):
        title = str(title) if title is not None else rel

    description = fm.get("description")
    description = str(description) if description is not None else None

    return Note(
        rel=rel,
        title=title,
        description=description,
        tags=normalize_tags(fm.get("tags")),
        timestamp=str(fm["timestamp"]) if fm.get("timestamp") else None,
        ava_agent=str(fm["ava_agent"]) if fm.get("ava_agent") else None,
        ava_machine=str(fm["ava_machine"]) if fm.get("ava_machine") else None,
        body=body,
        fm=fm,
    )


def walk_notes(
    root: Path,
    *,
    suffix: str = ".md",
    skip_names: frozenset[str] = frozenset(),
    warnings: list[str] | None = None,
) -> Iterator[tuple[Path, Note]]:
    """Yield `(path, note)` for every valid note under `root`, in sorted order.

    Files whose name is in `skip_names` are skipped; an unreadable file appends
    a `cannot read <rel>` warning (when `warnings` is given) and is skipped; a
    file without valid frontmatter is skipped silently. `note.rel` is the
    posix path relative to `root` sans `suffix`.
    """
    for md_file in sorted(root.rglob(f"*{suffix}")):
        if md_file.name in skip_names:
            continue
        rel = md_file.relative_to(root).as_posix()
        rel_id = rel[: -len(suffix)] if rel.endswith(suffix) else rel
        try:
            text = md_file.read_text(encoding="utf-8")
        except Exception:
            if warnings is not None:
                warnings.append(f"cannot read {rel}")
            continue
        note = parse_note(text, rel_id)
        if note is None:
            continue
        yield md_file, note


def extract_md_links(
    body: str,
    source_dir: Path,
    root: Path,
    source_id: str,
) -> tuple[tuple[str, str], ...]:
    """Markdown links in `body` → `(source_id, target_id)` edges.

    Links resolve relative to the source file's directory (standard Markdown
    resolution). `http(s)://`, `#` and empty targets are skipped; a target
    that resolves outside `root` (`../`, an absolute path) is skipped silently
    instead of raising — one bad link must not 500 the whole graph.
    """
    edges: list[tuple[str, str]] = []
    for match in _MD_LINK_RE.finditer(body):
        target = match.group(2)
        if not target or target.startswith(("http://", "https://", "#")):
            continue
        with suppress(ValueError):
            target_path = (source_dir / target).resolve().relative_to(root.resolve())
            edges.append((source_id, str(target_path.with_suffix(""))))
    return tuple(edges)
