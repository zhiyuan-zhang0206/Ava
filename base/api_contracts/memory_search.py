"""Wire vocabulary of POST /api/memory/search, shared by the SDK client, the gateway and the ranker."""

from __future__ import annotations

from typing import Literal

MemorySearchPurpose = Literal["recall", "search", "dedup"]
"""Why the query was issued. `recall`: passive recall, the query is the recent conversation.
`search`: an agent's own `ava.memory.search` call, the query is what the agent is looking for.
`dedup`: the check on a note an agent just wrote, the query is the new note and the score is
the probability that a result records essentially the same knowledge.
The ranker asks a different question for each."""
