"""Storage-facing node shapes --- a leaf module on purpose.

`MaterializedNode` is what a build hands to `store.write_tree`; the storage and
serving sides name the same shape. Keeping it (and the version constants in
the package `__init__`) out of `pipeline.py` keeps the LLM generation stack out
of every import closure that only stores or serves nodes --- the gateway
process must not pull `shared.lm` (tests/shared/test_gateway_consumer_guard.py;
the closure grew through `store` until this split).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MaterializedNode:
    """One node's outcome, storage-ready."""

    nid: str
    level: int
    kind: str  # group | alias
    span: tuple[int, int]
    at: tuple[str, str]
    trigger: str
    src_tok: int
    text: str
    text_hash: str  # sha256 of the text (idempotent-write key)
    input_hash: str  # the generation cache key this text was produced for
    children: tuple[str, ...]  # child uids, in stream order
    children_spans: tuple[tuple[int, int], ...]  # child spans, same order
