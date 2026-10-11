"""Wire vocabulary of POST /api/memory/search, shared by the SDK client, the gateway and the ranker."""

from __future__ import annotations

from typing import Literal

MemorySearchPurpose = Literal["recall", "search"]
"""Why the query was issued. `recall`: passive recall, the query is the recent conversation.
`search`: an agent's own `ava.memory.search` call, the query is what the agent is looking for.
The ranker asks a different relevance question for each."""
