"""
Long-term notes as markdown files, with semantic search to find them.

Two stores:

- Shared pool (`ava.memory.PATH`): notes visible to every agent. Restrained:
  reusable rules, repeatedly-referenced facts, and user rulings only; events
  stay out by default (git history already carries them).
- Per-agent memory (`<workspace>/memory/`): your own durable state.
  `MEMORY.md` there is the index — one line per memory, injected into your
  context at cold start and after each compact; each memory is one file
  beside it, read on demand. A write lands in the entry file and its index
  line.

Both stores are written through the same entry point, which resolves an
absolute store-owned path — immune to `ava.cwd` changes.

"""

from __future__ import annotations

try:
    import fcntl
except ImportError:  # Windows ships no fcntl module; the index lock degrades (see _locked_update)
    fcntl = None  # type: ignore[assignment]
import os
import re
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import ava as _ava
import shared.machine
import shared.paths
from ava import _gateway_client as _client
from ava._sdk_validation import coerce_str, coerce_typed
from shared.agents import IndexerUnavailable as IndexerUnavailable
from shared.paths import ava_home as _ava_home

__all_for_ava__ = ["PATH", "search", "write"]


_PATH_DOC = """When the pool spans machines, notes sync within about a day — a path `search`
returns may not have arrived here yet; retry later.

Start each note with YAML frontmatter, then the attribution header:

    ---
    type: Memory
    ava_agent: <your id>
    ---
    <!-- agent-<your id> @ <your machine>, YYYY-MM-DD HH:MM -->

Use `write(slug, content, ..., store="shared")` as the canonical writer:
an absolute pool path, immune to `ava.cwd` changes."""

PATH = _ava.const(_ava_home() / "memory", doc=_PATH_DOC)


def _search(
    query: str, k: int = 5, *, timeout: float | None = None
) -> list[tuple[Path, str, list[str]]]:
    """Semantic search; return the most relevant notes as (absolute path,
    description, tags) tuples. The description is "" when absent; tags carry
    the note's `type/<x>` tag.

    `timeout` bounds one attempt (seconds) — default is the gateway's own
    search deadline plus a 3s margin (18s). Under a congested index the
    gateway answers 503 (`IndexerUnavailable`) in about a second instead of
    queueing the request, so an explicit search degrades fast instead of
    piling up behind the fleet's shared gate. Pass a value only when the
    default is wrong for this call; keep it above
    `AVA_MEMORY_SEARCH_DEADLINE_SECONDS`, or the caller reads out first.
    """
    query = coerce_str(query, "query")
    k = coerce_typed(k, "k", int)
    timeout = coerce_typed(timeout, "timeout", (int, float), allow_none=True)
    results = _client.memory_search(query, k, timeout=timeout)
    return [(PATH / r.path, r.description, list(r.tags)) for r in results]


# Public binding — the plugin's wrap("memory.search", ...) replaces this name,
# leaving the private implementation (`_search`) untouched for validation tests.
search = _search


_PERSONAL_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def _entry_path(slug: str, store: str, agent_id: int) -> tuple[Path, bool]:
    """Resolve a memory entry to its store-owned absolute path.

    Shared entries may use topic directories, but neither store accepts a path
    that can escape its root. Personal names are intentionally narrower because
    that index is the agent's stable, flat namespace.
    """
    if not slug or slug.endswith(".md"):
        raise ValueError("memory slug must be a non-empty filename without .md")
    relative = Path(slug)
    if (
        relative.is_absolute()
        or "\\" in slug
        or any(part in {".", ".."} for part in relative.parts)
    ):
        raise ValueError("memory slug must be a relative path inside its store")
    if store == "personal":
        if not _PERSONAL_SLUG_RE.fullmatch(slug):
            raise ValueError("personal memory slug must be one kebab-case name without slashes")
        return shared.paths.workspace_dir(agent_id) / "memory" / f"{slug}.md", False
    if store == "shared":
        if relative == Path("MEMORY"):
            raise ValueError("shared memory slug cannot replace MEMORY.md")
        return shared.paths.memory_dir() / relative.with_suffix(".md"), True
    raise ValueError("memory store must be 'personal' or 'shared'")


def _validated_tags(tags: list[str] | None) -> list[str]:
    """Return tags after enforcing the one-type-tag memory invariant."""
    values = ["type/reference"] if tags is None else list(tags)
    if sum(tag.startswith("type/") and len(tag) > len("type/") for tag in values) != 1:
        raise ValueError("memory tags must contain exactly one type/<x> tag")
    return values


def _write_atomically(path: Path, content: str) -> None:
    """Replace one memory entry without exposing a partially written note."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(content)
        os.replace(temporary, path)  # noqa: PTH105 — atomic publication required by the memory contract
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


_INDEX_DESCRIPTION_MAX = 120
"""Cap on the description text rendered into a MEMORY.md pointer line.

The index is injected into agent context at cold start and after each compact,
so an unbounded description would leak into every agent's context budget — the
one unbounded input on this path. The note itself keeps the full text; only
the index line is truncated."""


def _pointer_line(title: str, relative_path: str, description: str) -> str:
    """Render the one-line index entry for a durable memory note."""
    if len(description) > _INDEX_DESCRIPTION_MAX:
        description = description[:_INDEX_DESCRIPTION_MAX].rstrip() + "..."
    return f"- [{title}]({relative_path}) — {description or title}"


def _locked_update(index_path: Path, update: Callable[[str], str]) -> None:
    """Apply one index update while holding its advisory per-file lock.

    POSIX: fcntl.flock, unchanged. Windows has no fcntl module, so the lock
    degrades to a no-op there — the update itself still runs, unguarded. The
    lock is advisory and each upsert rewrites a single pointer line in place,
    so an unlocked Windows update can at worst drop one line under a
    concurrent writer; on a single-user box that beats crashing every agent at
    plugin load (unguarded import introduced by 6e96b1554)."""
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("a+", encoding="utf-8") as index_file:
        if fcntl is not None:
            fcntl.flock(index_file.fileno(), fcntl.LOCK_EX)
        try:
            index_file.seek(0)
            text = update(index_file.read())
            index_file.seek(0)
            index_file.truncate()
            index_file.write(text)
            index_file.flush()
        finally:
            if fcntl is not None:
                fcntl.flock(index_file.fileno(), fcntl.LOCK_UN)


def _upsert_index(
    root: Path, relative_path: str, title: str, description: str, *, shared: bool
) -> None:
    """Replace or append one index pointer without disturbing other entries."""
    index_path = root / "MEMORY.md"
    pointer = _pointer_line(title, relative_path, description)
    target = re.compile(rf"^- \[[^]]+\]\({re.escape(relative_path)}\) — .*$")

    def update(text: str) -> str:
        lines = text.splitlines()
        matches = [index for index, line in enumerate(lines) if target.fullmatch(line)]
        if matches:
            lines[matches[0]] = pointer
            for index in reversed(matches[1:]):
                del lines[index]
        elif shared and "## Pointers" in lines:
            section_start = lines.index("## Pointers")
            section_end = next(
                (
                    index
                    for index in range(section_start + 1, len(lines))
                    if lines[index].startswith("## ")
                ),
                len(lines),
            )
            pointer_lines = [
                index
                for index in range(section_start + 1, section_end)
                if lines[index].startswith("- [")
            ]
            lines.insert(pointer_lines[-1] + 1 if pointer_lines else section_start + 1, pointer)
        else:
            lines.append(pointer)
        return "\n".join(lines) + "\n"

    _locked_update(index_path, update)


def write(
    slug: str,
    content: str,
    *,
    title: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    store: str = "personal",
) -> Path:
    """Upsert the store's MEMORY.md pointer.

    Personal entries use a flat kebab-case name in the calling agent's
    workspace; shared entries may use topic directories in the memory pool.
    Both targets are absolute store paths.

    Each index update holds an advisory lock on the store's `MEMORY.md`, so
    concurrent writers in this and other processes are serialized.
    """
    slug = coerce_str(slug, "slug")
    content = coerce_str(content, "content")
    title = coerce_str(title, "title", allow_none=True)
    description = coerce_str(description, "description", allow_none=True)
    tags = coerce_typed(tags, "tags", (list, tuple), allow_none=True)
    store = coerce_str(store, "store")
    from ava._boot import _agent_id

    agent_id = _agent_id
    if agent_id is None:
        raise RuntimeError("ava.memory.write requires an established agent id")
    entry, is_shared = _entry_path(slug, store, agent_id)
    values = _validated_tags(tags)
    note_title = title or slug
    note_description = description or ""
    if is_shared:
        now = datetime.now(UTC)
        machine = shared.machine.machine_name()
        frontmatter = (
            f"---\ntype: Memory\nava_agent: {agent_id}\ntitle: {note_title}\n"
            f"description: {note_description}\ntags: [{', '.join(values)}]\n"
            f"timestamp: '{now.isoformat()}'\nava_machine: {machine}\n---\n"
            f"<!-- agent-{agent_id} @ {machine}, {now:%Y-%m-%d %H:%M} -->\n\n"
        )
    else:
        frontmatter = (
            f"---\nname: {slug}\ndescription: {note_description}\n"
            f"tags: [{', '.join(values)}]\n---\n\n"
        )
    _write_atomically(entry, frontmatter + content)
    root = (
        shared.paths.memory_dir() if is_shared else shared.paths.workspace_dir(agent_id) / "memory"
    )
    _upsert_index(
        root, entry.relative_to(root).as_posix(), note_title, note_description, shared=is_shared
    )
    return entry.resolve()
