"""Memory search and graph HTTP contracts."""

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
)

from base.api_contracts.memory_search import MemorySearchPurpose


class MemorySearchRequest(BaseModel):
    """POST /api/memory/search request — semantic search query."""

    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] = Field(
        ..., description="natural-language query"
    )
    k: int = Field(default=5, ge=1, le=100, description="result count")
    purpose: MemorySearchPurpose = Field(
        ...,
        description=(
            "why the query was issued: 'recall' (passive recall; the query is the recent "
            "conversation), 'search' (an agent's own search) or 'dedup' (the duplicate check on "
            "a note an agent just wrote; the query is that note); selects the ranking question"
        ),
    )


class MemorySearchResultItem(BaseModel):
    """A single search result: file path, the chunk that matched, its relevance
    score, and the frontmatter a caller can judge the hit by without opening it."""

    path: str = Field(..., description="relative path from memory pool root")
    description: str = Field(default="", description="frontmatter description field, or empty")
    tags: list[str] = Field(
        default_factory=list,
        description="frontmatter tags, including the note's `type/<x>`, or empty",
    )
    score: float = Field(
        ..., ge=0, le=1, description="probability that the matched chunk is relevant to the query"
    )
    chunk: str = Field(
        ...,
        description="text of the note's best-matching chunk (or its description line), as indexed",
    )


class MemorySearchResponse(BaseModel):
    """POST /api/memory/search response — RELATIVE paths from memory pool root.

    Caller (SDK `ava.memory.search`) uses `ava.memory.PATH / p` to
    reconstruct absolute paths. fs-neutral makes mismatched gateway
    (e.g. /Users/x) and agent-runner (/home/y) filesystems work.

    `results` carries path, description, tags, score and matched chunk for each
    match, most relevant first.
    """

    results: list[MemorySearchResultItem]


class MemoryRefreshResponse(BaseModel):
    """POST /api/memory/refresh response."""

    head: str  # HEAD commit sha of the gateway memory checkout after fast-forward to origin/main


class MemoryGraphNode(BaseModel):
    """One node in the OKF memory graph — a concept note, or a folder pseudo
    node (the graph's structural skeleton)."""

    model_config = ConfigDict(frozen=True)

    id: str
    path: str
    title: str
    kind: Literal["note", "folder"]
    description: str | None
    tags: list[str]
    primary_tag: str
    timestamp: str | None
    ava_agent: str | None
    ava_machine: str | None


class MemoryGraphEdge(BaseModel):
    """One directed edge in the OKF memory graph.

    `kind` separates the two edge families the frontend renders differently:
    `containment` (note → folder, folder → parent folder — the main
    structure) and `reference` (a markdown cross-link between two notes).
    """

    model_config = ConfigDict(frozen=True)

    source: str
    target: str
    kind: Literal["containment", "reference"]


class MemoryGraphResponse(BaseModel):
    """GET /api/memory/graph response."""

    model_config = ConfigDict(frozen=True)

    nodes: list[MemoryGraphNode]
    edges: list[MemoryGraphEdge]
    warnings: list[str] = Field(default_factory=list)


class MemoryNoteResponse(BaseModel):
    """GET /api/memory/note response — one parsed memory note.

    Mirrors MemoryGraphNode plus the parsed markdown body. The body is the
    markdown with the YAML frontmatter removed (base.parse_note's body), so
    the frontend renders the note itself rather than re-parsing frontmatter
    (frontmatter values arrive as structured fields: title / description /
    tags / timestamp / ava_agent / ava_machine).
    """

    model_config = ConfigDict(frozen=True)

    path: str
    title: str
    description: str | None
    tags: list[str]
    timestamp: str | None
    ava_agent: str | None
    ava_machine: str | None
    body: str
