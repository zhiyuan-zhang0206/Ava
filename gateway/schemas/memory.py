"""memory search / graph.

Split out of the former monolithic ops/schemas.py; FastAPI registers these
unchanged, so the OpenAPI codegen is byte-identical to the wire before.
"""

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)


class MemorySearchRequest(BaseModel):
    """POST /api/memory/search request — semantic search query."""

    query: str = Field(..., min_length=1, description="natural-language query")
    k: int = Field(default=5, ge=1, le=100, description="top-k result count")


class MemorySearchResultItem(BaseModel):
    """A single search result: file path + the frontmatter a caller can judge the
    hit by without opening it."""

    path: str = Field(..., description="relative path from memory pool root")
    description: str = Field(default="", description="frontmatter description field, or empty")
    tags: list[str] = Field(
        default_factory=list,
        description="frontmatter tags, including the note's `type/<x>`, or empty",
    )


class MemorySearchResponse(BaseModel):
    """POST /api/memory/search response — RELATIVE paths from memory pool root.

    Caller (SDK `ava.memory.search`) uses `ava.memory.PATH / p` to
    reconstruct absolute paths. fs-neutral makes mismatched gateway
    (e.g. /Users/x) and agent-runner (/home/y) filesystems work.

    `results` carries path + description for each match; `paths` is the
    bare list of relative paths (backward-compat for existing consumers).
    """

    paths: list[str] = Field(default_factory=list)
    results: list[MemorySearchResultItem] = Field(default_factory=list)


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
    markdown with the YAML frontmatter removed (shared.parse_note's body), so
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
