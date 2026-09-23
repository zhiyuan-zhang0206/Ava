"""Long-term notes as markdown files, with semantic search to find them."""

from __future__ import annotations

try:
    import fcntl
except ImportError:  # Windows ships no fcntl module; the index lock degrades (see _locked_update)
    fcntl = None  # type: ignore[assignment]
import os
import re
import tempfile
from collections.abc import Callable, Sized
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import yaml

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


_INDEX_RESERVED = frozenset({"MEMORY.md", "AGENTS.md", "index.md", "log.md"})
"""Filenames a directory index never lists as note entries (generator convention)."""

_POINTER_TARGET_RE = re.compile(r"\]\(([^)#]+?\.md)\)")
"""Target filename of a markdown pointer line, as the pool validator reads it."""


def _pointer_target(line: str) -> str | None:
    """Target filename of a bullet pointer line, or None for other lines.

    The first `](name.md)` link on the line is its target — the same read the
    pool validator and `_insert_dir_pointer` use. Reading the target instead
    of pattern-matching the bracketed title keeps the lookup working for any
    title text: titles may contain `]`, e.g. `md5[:12]`."""
    if not line.lstrip().startswith(("*", "-")):
        return None
    match = _POINTER_TARGET_RE.search(line)
    return match.group(1) if match is not None else None


def _dir_pointer_line(title: str, filename: str, description: str) -> str:
    """Render one directory-index entry (gen_indexes.py shape, no truncation)."""
    description = description.replace("\n", " ")
    return f"* [{title}]({filename}) - {description}"


def _disk_frontmatter(path: Path) -> dict[str, object]:
    """Frontmatter of a note already on disk; empty when absent or unreadable."""
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    parts = _frontmatter_parts(content)
    if parts is None:
        return {}
    try:
        parsed = yaml.safe_load(parts[0])
    except yaml.YAMLError:
        return {}
    return cast("dict[str, object]", parsed) if isinstance(parsed, dict) else {}


def _count_dir_notes(directory: Path) -> int:
    """Recursive count of the note files under a subdirectory (generator's rule)."""
    return sum(
        1
        for path in directory.rglob("*.md")
        if path.name not in _INDEX_RESERVED
        and ".git" not in path.parts
        and ".githooks" not in path.parts
    )


def _render_dir_index(directory: Path, dir_rel: str) -> str:
    """Render a directory's index.md in the consolidation generators' shape.

    Mirrors gen_indexes.py write_index: entries sort by filename, subdirectory
    counts are recursive, descriptions flatten to one line, and a note whose
    frontmatter is missing falls back to its file name."""
    entries = sorted(path.name for path in directory.iterdir())
    subdirs = [
        entry for entry in entries if (directory / entry).is_dir() and not entry.startswith(".")
    ]
    filenames = [
        entry for entry in entries if entry.endswith(".md") and entry not in _INDEX_RESERVED
    ]
    lines = [f"# {dir_rel}/", "", "## Subdirectories", ""]
    for subdir in subdirs:
        lines.append(f"* [{subdir}/]({subdir}/) - {_count_dir_notes(directory / subdir)} notes")
    if not subdirs:
        lines.append("*(none)*")
    lines.extend(["", "## Notes", ""])
    for filename in filenames:
        frontmatter = _disk_frontmatter(directory / filename)
        title = str(frontmatter.get("title") or filename[:-3])
        description = str(frontmatter.get("description") or "").replace("\n", " ")
        lines.append(f"* [{title}]({filename}) - {description}")
    if not filenames:
        lines.append("*(none)*")
    lines.append("")
    return "\n".join(lines)


def _insert_dir_pointer(lines: list[str], pointer: str, filename: str) -> str:
    """Insert one note pointer into a `## Notes` section, filename-sorted."""
    notes_start = lines.index("## Notes") if "## Notes" in lines else None
    if notes_start is None:
        while lines and not lines[-1].strip():
            lines.pop()
        lines.extend(["", "## Notes", "", pointer])
        return "\n".join(lines) + "\n"
    section_end = next(
        (index for index in range(notes_start + 1, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    entries: list[tuple[int, str]] = []
    for index in range(notes_start + 1, section_end):
        target = _POINTER_TARGET_RE.search(lines[index])
        if target is not None and lines[index].lstrip().startswith(("*", "-")):
            entries.append((index, target.group(1)))
    if not entries:
        placeholder = next(
            (
                index
                for index in range(notes_start + 1, section_end)
                if lines[index].strip() == "*(none)*"
            ),
            None,
        )
        if placeholder is not None:
            lines[placeholder] = pointer
        else:
            insert_at = notes_start + 1
            while insert_at < section_end and not lines[insert_at].strip():
                insert_at += 1
            lines.insert(insert_at, pointer)
        return "\n".join(lines) + "\n"
    insert_at = entries[-1][0] + 1
    for index, target_name in entries:
        if target_name > filename:
            insert_at = index
            break
    lines.insert(insert_at, pointer)
    return "\n".join(lines) + "\n"


def _upsert_subdir_index(root: Path, relative_path: str, title: str, description: str) -> None:
    """Upsert one note's line in its own directory's index.md.

    The root index carries root-level notes and directory pointers only; a
    shared subdirectory entry's line belongs to the directory's own index, in
    the generators' shape (`* [title](file.md) - description`). A missing
    index is rendered as the full skeleton; an existing one has only its one
    pointer line replaced, or inserted in filename order."""
    dir_rel, _, filename = relative_path.rpartition("/")
    directory = root / dir_rel
    pointer = _dir_pointer_line(title, filename, description)

    def update(text: str) -> str:
        if not text.strip():
            return _render_dir_index(directory, dir_rel)
        lines = text.splitlines()
        matches = [index for index, line in enumerate(lines) if _pointer_target(line) == filename]
        if matches:
            lines[matches[0]] = pointer
            for index in reversed(matches[1:]):
                del lines[index]
            return "\n".join(lines) + "\n"
        return _insert_dir_pointer(lines, pointer, filename)

    _locked_update(directory / "index.md", update)


def _upsert_index(
    root: Path, relative_path: str, title: str, description: str, *, shared: bool
) -> None:
    """Replace or append one index pointer without disturbing other entries.

    Shared subdirectory entries are routed to their own directory's index.md:
    the root index carries root-level notes and directory pointers only, and
    the entry's directory index is upserted in the generators' shape.
    """
    if shared and "/" in relative_path:
        _upsert_subdir_index(root, relative_path, title, description)
        return
    index_path = root / "MEMORY.md"
    pointer = _pointer_line(title, relative_path, description)

    def update(text: str) -> str:
        lines = text.splitlines()
        matches = [
            index for index, line in enumerate(lines) if _pointer_target(line) == relative_path
        ]
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


_FRONTMATTER_OPEN = "---\n"


def _frontmatter_parts(content: str) -> tuple[str, str] | None:
    """Split a leading frontmatter block into (block, rest); None when the
    content carries none. Mirrors the split the pool's validator reads with."""
    if not content.startswith(_FRONTMATTER_OPEN):
        return None
    parts = content.split(_FRONTMATTER_OPEN, 2)
    if len(parts) < 3:
        return None
    return parts[1], parts[2]


def _parse_frontmatter(block: str) -> dict[str, object]:
    """Parse a caller-provided block; it must be a non-empty YAML mapping."""
    try:
        parsed = yaml.safe_load(block)
    except yaml.YAMLError as error:
        raise ValueError(
            "content frontmatter is not valid YAML - fix the block or drop it"
        ) from error
    if not isinstance(parsed, dict) or not parsed:
        raise ValueError(
            "content opens with a frontmatter block that is not a non-empty "
            "YAML mapping - write `key: value` fields or drop the block"
        )
    return cast("dict[str, object]", parsed)


def _filled(value: object) -> bool:
    """Whether a frontmatter value counts as present (blank strings do not)."""
    if isinstance(value, str):
        return bool(value.strip())
    if value is None:
        return False
    if isinstance(value, Sized):
        return len(value) > 0
    return True


def _first_text(*values: object) -> str | None:
    """First non-blank string among the candidates."""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return None


_YAML_SENSITIVE_START = "#\"'@`%*&!|>[]{},?-:"


def _quote_text(value: str) -> str:
    """Render a text value as a double-quoted YAML string."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _yaml_value(value: str) -> str:
    """Render a value so YAML reads it back exactly as written.

    Printed plain, a value must parse back as itself: ` #` opens a comment,
    `: ` or a trailing colon breaks the mapping, a leading indicator changes
    the parse, and bare `null`, booleans, numbers or dates come back as
    another type. Anything failing that read-back is double-quoted."""
    return value if _reads_back_plain(value) else _quote_text(value)


def _reads_back_plain(value: str) -> bool:
    """Whether YAML parses `value` back as exactly this string, unquoted."""
    if not value or value != value.strip() or value[0] in _YAML_SENSITIVE_START:
        return False
    if ": " in value or " #" in value or value.endswith(":") or "\n" in value:
        return False
    try:
        parsed = yaml.safe_load(value)
    except yaml.YAMLError:
        return False
    return isinstance(parsed, str) and parsed == value


def _load_value(value: str) -> object:
    """The single-value YAML read of a text, or None when it does not parse."""
    try:
        return yaml.safe_load(value)
    except yaml.YAMLError:
        return None


_YAML_OWN_START = "|>&*!"


def _flow_closes(value: str) -> bool:
    """Whether a value opening `[` or `{` closes its flow on the same line."""
    return value.count("[") == value.count("]") and value.count("{") == value.count("}")


def _quote_closes(value: str) -> bool:
    """Whether a value opening `'` or `"` closes its quote on the same line."""
    quote = value[0]
    index = 1
    while index < len(value):
        char = value[index]
        if quote == '"' and char == "\\":
            index += 2
            continue
        if char == quote:
            if quote == "'" and index + 1 < len(value) and value[index + 1] == "'":
                index += 2
                continue
            return True
        index += 1
    return False


def _caller_value(value: str) -> str | None:
    """A caller's own text for one value line, or None when the line already
    reads back as written.

    A quoted string, a complete flow collection, a block scalar header, an
    anchor, an alias, a tag or a comment is the caller's YAML and stays as
    it is; so does a quote or a flow that does not close on its line, which
    may still be a multi-line scalar the block read accepts. Anything else
    that would not read back as the written text - truncated at a ` #`,
    rejected as a `: ` or a trailing colon, or retyped like `null`, `123`
    or a date - is quoted on the same terms as a generated value."""
    if not value or value[0] == "#" or value[0] in _YAML_OWN_START:
        return None
    if value[0] in "\"'" and (isinstance(_load_value(value), str) or not _quote_closes(value)):
        return None
    if value[0] in "[{" and (
        isinstance(_load_value(value), (list, dict)) or not _flow_closes(value)
    ):
        return None
    if _reads_back_plain(value):
        return None
    return _quote_text(value)


_CALLER_VALUE_LINE = re.compile(r"(?P<key>[A-Za-z0-9_.-]+):(?P<gap>[ \t]+)(?P<value>\S.*)")


def _quote_caller_values(block: str) -> str:
    """Quote each value line of a caller's block that YAML would not read
    back as written.

    Line order, blank lines and comments stay put, and a line that already
    reads back as written - including an already-quoted one - is left
    byte-identical. Only the block's own unindented `key: value` lines are
    inspected; an indented line under a key is left to YAML. A ` # note`
    inside a bare value is part of it - the value is quoted so that text
    survives as written."""
    lines = block.split("\n")
    for index, line in enumerate(lines):
        match = _CALLER_VALUE_LINE.match(line)
        if match is None:
            continue
        quoted = _caller_value(match.group("value").rstrip())
        if quoted is None:
            continue
        lines[index] = f"{match.group('key')}:{match.group('gap')}{quoted}"
    return "\n".join(lines)


_TIMESTAMP_FRACTION_RE = re.compile(r"(?<=T\d{2}:\d{2}:\d{2})\.\d+")


def _drop_timestamp_fraction(block: str) -> str:
    """Strip sub-second digits from a caller's timestamp - the pool stores
    second precision and its validator rejects a `12:00:00.5` stamp."""
    if "timestamp:" not in block:
        return block
    return "\n".join(
        _TIMESTAMP_FRACTION_RE.sub("", line) if line.startswith("timestamp:") else line
        for line in block.split("\n")
    )


def _merge_frontmatter(
    block: str, existing: dict[str, object], generated: list[tuple[str, str]]
) -> str:
    """Keep the caller's block text and complete the fields it is missing.

    A blank value is replaced in place; only a field with no line at all is
    appended, so a completed block never carries the same key twice."""
    lines = block.split("\n")
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    for key, value in generated:
        if _filled(existing.get(key)):
            continue
        replacement = f"{key}: {value}"
        for index, line in enumerate(lines):
            if line.startswith(f"{key}:"):
                lines[index] = replacement
                break
        else:
            lines.append(replacement)
    return "\n".join(lines) + "\n"


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

    Personal entries use a flat kebab-case name in your workspace; shared entries
    may use topic directories in the memory pool. A shared subdirectory entry is
    indexed by its own directory's `index.md` (created when missing), leaving the
    root `MEMORY.md` untouched. The entry always ends with a newline; a missing one is appended.

    Content may open with its own frontmatter block — kept as the note's only
    one, gaining any missing required fields; otherwise the writer generates the
    block (plus the attribution line on the shared store). A missing title
    defaults to the file name, a missing description to the title.
    """
    slug = coerce_str(slug, "slug")
    content = coerce_str(content, "content")
    title = coerce_str(title, "title", allow_none=True)
    description = coerce_str(description, "description", allow_none=True)
    tags = coerce_typed(tags, "tags", (list, tuple), allow_none=True)
    store = coerce_str(store, "store")
    from ava._boot import require_agent_id

    agent_id = require_agent_id()
    entry, is_shared = _entry_path(slug, store, agent_id)
    values = _validated_tags(tags)

    parts = _frontmatter_parts(content)
    if parts is None:
        block, rest = None, content
        existing: dict[str, object] = {}
    else:
        block, rest = parts
        block = _quote_caller_values(block)
        existing = _parse_frontmatter(block)
        block = _drop_timestamp_fraction(block)
    note_title = _first_text(existing.get("title"), title) or entry.stem
    note_description = _first_text(existing.get("description"), description) or note_title

    now = datetime.now(UTC).replace(microsecond=0)
    if is_shared:
        machine = shared.machine.machine_name()
        generated = [
            ("type", "Memory"),
            ("ava_agent", str(agent_id)),
            ("title", _yaml_value(note_title)),
            ("description", _yaml_value(note_description)),
            ("tags", f"[{', '.join(_yaml_value(tag) for tag in values)}]"),
            ("timestamp", f"'{now.isoformat()}'"),
            ("ava_machine", _yaml_value(machine)),
        ]
        attribution = f"<!-- agent-{agent_id} @ {machine}, {now:%Y-%m-%d %H:%M} -->\n\n"
    else:
        generated = [
            ("name", _yaml_value(slug)),
            ("description", _yaml_value(note_description)),
            ("tags", f"[{', '.join(_yaml_value(tag) for tag in values)}]"),
        ]
        attribution = "\n"

    if block is None:
        header = "".join(f"{key}: {value}\n" for key, value in generated)
        written = f"---\n{header}---\n{attribution}{rest}"
    else:
        merged = _merge_frontmatter(block, existing, generated)
        written = f"---\n{merged}---\n{rest}"
    if not written.endswith("\n"):
        # The pool treats a terminal newline as canonical; normalize at the single
        # write entry point so callers cannot publish entries a sweep must repair.
        written += "\n"
    _write_atomically(entry, written)
    root = (
        shared.paths.memory_dir() if is_shared else shared.paths.workspace_dir(agent_id) / "memory"
    )
    _upsert_index(
        root, entry.relative_to(root).as_posix(), note_title, note_description, shared=is_shared
    )
    return entry.resolve()
