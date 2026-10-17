"""Wire vocabulary of POST /api/memory/search, shared by the SDK client, the gateway and the ranker."""

from __future__ import annotations

from typing import Literal

MemorySearchPurpose = Literal["recall", "search", "dedup"]
"""Why the query was issued. `recall`: passive recall, the query is the recent conversation.
`search`: an agent's own `meiva.memory.search` call, the query is what the agent is looking for.
`dedup`: the check on a note an agent just wrote, the query is the new note and the score is
the probability that a result records essentially the same knowledge.
The ranker asks a different question for each."""


MEMORY_SEARCH_MAX_EXCLUDE = 800
"""Most paths one search request may exclude. Vector search is asked for the candidates plus
every excluded note, and the search service answers at most 1000 notes per query, so this plus
the largest `memory_rerank_candidates` (100) must stay within that."""

MEMORY_SEARCH_MAX_PATH_CHARS = 1024
"""Longest memory-pool-relative path the index stores."""


def validate_pool_path(path: str) -> str:
    """`path` when it is a normalized memory-pool-relative path, else `ValueError`.

    The form the index returns: forward slashes, no leading slash, no empty, `.` or `..`
    segments, no control characters.
    """
    if path.startswith("/") or path.endswith("/") or "\\" in path:
        raise ValueError(f"not a memory-pool-relative path: {path!r}")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in path):
        raise ValueError("a path holds no control characters")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise ValueError(f"not a normalized memory-pool-relative path: {path!r}")
    return path
