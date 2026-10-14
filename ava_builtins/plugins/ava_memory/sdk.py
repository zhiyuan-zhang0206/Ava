"""Long-term notes as markdown files, with semantic search to find them.

A note is a markdown file with a YAML frontmatter block. There is no writer function: create and
update notes with `ava.files.write` and `ava.files.edit` under `PATH` (notes every agent can read)
or `PERSONAL_PATH` (your own), following the format described on those two paths.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import NamedTuple

import ava as _ava
import base.paths
from ava import gateway_client as _client
from ava.sdk_surface.validation import coerce_str, coerce_typed
from base.api_contracts.agent_contract import IndexerUnavailable as IndexerUnavailable
from base.paths import ava_home as _ava_home

__all_for_ava__ = ["PATH", "PERSONAL_PATH", "MemoryMatch", "search"]


_NOTE_FORMAT = """A note is a .md file: a YAML frontmatter block, then the body.

    ---
    type: Memory
    ava_agent: <your agent id>
    ava_machine: <your machine name>
    timestamp: '<current time, ISO 8601 with offset, e.g. 2026-01-31T09:30:00+00:00>'
    title: <short title>
    description: <one line saying what the note holds; search results and indexes show it>
    tags: [type/<kind>]
    ---
    <!-- agent-<your agent id> @ <your machine name>, YYYY-MM-DD HH:MM -->

    The note text.

Your agent id is `ava.self.AGENT_ID` and your machine name is `ava.self.MACHINE_SPEC.name`.
`type`, `ava_agent`, `description` and exactly one `type/<kind>` tag are required; the kind is one
of user, feedback, project, reference, env, role. Put a value in double quotes when it contains
": " or " #". Keep a note under 8000 characters and on one topic. Search for an existing note
before writing a new one and update it in place when it already covers the ground.

Notes you write are checked after the call that writes them; a reminder names any rule a note
breaks, and any shared note that looks like a duplicate of it."""

_PATH_DOC = (
    """Root folder of the shared notes that every agent can read. When the pool spans machines,
notes sync within about a day, so a path `search` returns may not have arrived here yet; retry
later.

Write a note with `ava.files.write(ava.memory.PATH / "topic/name.md", text)`; use the absolute
path so a changed `ava.cwd` cannot misplace it. A topic subfolder is fine.
"""
    + "\n"
    + _NOTE_FORMAT
)

_PERSONAL_PATH_DOC = (
    """Root folder of your own notes: the memory folder of your workspace. Only you maintain it.
Keep one line per note in MEMORY.md in this folder, `- [Title](file.md) - description`; the
notes themselves stay beside it.

Write a note with `ava.files.write(ava.memory.PERSONAL_PATH / "name.md", text)`.
"""
    + "\n"
    + _NOTE_FORMAT
)


@functools.cache
def _documented_pool(home: Path) -> Path:
    """The pool root for `home`, wrapped once so every read of one home is one object."""
    return _ava.const(home / "memory", doc=_PATH_DOC)


def __getattr__(name: str) -> Path:
    """`PATH` and `PERSONAL_PATH`, resolved when read so the module never captures the home or
    the agent identity at import.

    An assignment (`memory.PATH = ...`, the isolated-evaluation boot in
    `agent/process_boot.py`) creates a real attribute that shadows this hook.
    """
    if name == "PATH":
        return _documented_pool(_ava_home())
    if name == "PERSONAL_PATH":
        from ava.sdk_surface.agent_identity import agent_id

        owner = agent_id()
        if owner is None:
            # An AttributeError, as for any name the module does not have: `hasattr` and the help
            # renderer ask about it before an identity exists.
            raise AttributeError("ava.memory.PERSONAL_PATH needs an established agent identity")
        return _ava.const(base.paths.workspace_dir(owner) / "memory", doc=_PERSONAL_PATH_DOC)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def pool_root() -> Path:
    """The pool root as `PATH` reads: an assigned value wins, else the home's."""
    return globals().get("PATH") or __getattr__("PATH")


class MemoryMatch(NamedTuple):
    """One search result: a note and the passage of it that matched.

    path: absolute path of the note.
    description: the note's description; empty when it has none.
    tags: the note's tags, including its `type/<x>` tag.
    score: probability from 0 to 1 that the passage is relevant to the query.
    chunk: the matched passage of the note, or its description line.
    """

    path: Path
    description: str
    tags: list[str]
    score: float
    chunk: str


def search_unwrapped(query: str, k: int = 5, *, timeout: float | None = None) -> list[MemoryMatch]:
    """Find shared memory notes relevant to a query.

    Return up to `k` matches, most relevant first. Each is judged on its best passage,
    so a high score means that passage is relevant, not that the whole note is. Matches
    are returned whatever their score; use `score` to decide how much weight a weak one
    deserves. `timeout` bounds each search attempt in seconds; omit it to use the
    configured limit."""
    query = coerce_str(query, "query")
    k = coerce_typed(k, "k", int)
    timeout = coerce_typed(timeout, "timeout", (int, float), allow_none=True)
    results = _client.memory_search(query, k, purpose="search", timeout=timeout)
    return [
        MemoryMatch(pool_root() / r.path, r.description, list(r.tags), r.score, r.chunk)
        for r in results
    ]


# Public binding — the plugin's wrap("memory.search", ...) replaces this name,
# leaving the private implementation (`search_unwrapped`) untouched for validation tests.
search = search_unwrapped
