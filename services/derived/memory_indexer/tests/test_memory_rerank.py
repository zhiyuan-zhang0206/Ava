"""The second search stage: restoring chunk text, batching, ranking and failure semantics.

The decision model is replaced by fakes at the two seams `rerank.py` confines it to
(`decision_limits` and `ask_relevance`) and, for the error mapping, at `decide` itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

import pytest

from base.api_contracts.agent_contract import IndexerUnavailable
from base.api_contracts.memory_search import MemorySearchPurpose
from base.config import settings
from base.lm.catalog import ModelCatalog
from base.lm.catalog.decide.types import (
    FALSE,
    TRUE,
    Decision,
    DecisionRequestError,
    YesNo,
)
from base.lm.plugin_providers import build_model_catalog
from services.derived.memory_indexer import rerank
from services.derived.memory_indexer.backends.base import KIND_BODY, KIND_DESC, ChunkHit
from services.derived.memory_indexer.chunking import chunk_text

_NOTE = """---
type: Memory
description: Gateway deploy runbook
tags:
- type/reference
- ops
---

First paragraph about restarting the gateway.

Second paragraph about rolling back.
"""


@pytest.fixture
def catalog() -> ModelCatalog:
    return build_model_catalog()


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _hit(root: Path, rel: str, kind: str = KIND_BODY, idx: int = 0) -> ChunkHit:
    return ChunkHit(str(root / rel), kind, idx, 0.5)


class _FakeJudge:
    """Stands in for the decision call: probability by a marker in the state, default 0.

    Records every call and the most requests that were in flight at once."""

    def __init__(self, rule: Mapping[str, float] | None = None, default: float = 0.0) -> None:
        self.rule = dict(rule or {})
        self.default = default
        self.calls: list[tuple[str, str, YesNo]] = []
        self.usage_kinds: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def __call__(
        self, model: str, state: str, question: YesNo, *, catalog: ModelCatalog, usage_kind: str
    ) -> float:
        del catalog
        self.calls.append((model, state, question))
        self.usage_kinds.append(usage_kind)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return next((p for marker, p in self.rule.items() if marker in state), self.default)
        finally:
            self.in_flight -= 1


def _install(monkeypatch: pytest.MonkeyPatch, judge: _FakeJudge) -> None:
    monkeypatch.setattr(rerank, "ask_relevance", judge)


async def _rank(
    tmp_path: Path,
    catalog: ModelCatalog,
    hits: list[ChunkHit],
    *,
    query: str = "q",
    purpose: MemorySearchPurpose = "recall",
    k: int = 5,
    limit: int = 30,
    exclude: frozenset[str] = frozenset(),
) -> list[rerank.RankedChunk]:
    return await rerank.rerank(
        query,
        hits,
        purpose=purpose,
        model="m",
        catalog=catalog,
        k=k,
        limit=limit,
        exclude=exclude,
        memory_root=tmp_path,
    )


# ── chunk_text ──


def test_chunk_text_restores_each_indexed_row() -> None:
    assert chunk_text(_NOTE, KIND_DESC, 0) == "Gateway deploy runbook"
    body = chunk_text(_NOTE, KIND_BODY, 0)
    assert body is not None
    assert "restarting the gateway" in body
    assert "rolling back" in body  # short note: one chunk holds both paragraphs


def test_chunk_text_is_none_for_a_row_the_file_no_longer_has() -> None:
    assert chunk_text(_NOTE, KIND_BODY, 5) is None
    assert chunk_text("no frontmatter, only body", KIND_DESC, 0) is None


def test_chunk_text_indexes_long_bodies_by_the_indexer_rules() -> None:
    paragraphs = [f"paragraph {i} " + "x" * 900 for i in range(6)]
    content = "---\ntype: Memory\n---\n\n" + "\n\n".join(paragraphs)
    first, second = chunk_text(content, KIND_BODY, 0), chunk_text(content, KIND_BODY, 1)
    assert first is not None and second is not None and first != second
    assert chunk_text(content, KIND_BODY, 99) is None


# ── ranking ──


async def test_rerank_orders_by_probability_and_returns_the_scored_chunk(
    tmp_path: Path, catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "ops/runbook.md", _NOTE)
    _write(tmp_path, "other.md", "---\ntype: Memory\n---\n\nLunch menu for Friday.")
    judge = _FakeJudge({"Lunch menu": 0.2, "restarting the gateway": 0.9})
    _install(monkeypatch, judge)

    ranked = await _rank(
        tmp_path,
        catalog,
        # vector order puts the irrelevant note first
        [_hit(tmp_path, "other.md"), _hit(tmp_path, "ops/runbook.md")],
        query="how do I restart the gateway",
    )

    assert [item.path for item in ranked] == ["ops/runbook.md", "other.md"]
    assert [item.score for item in ranked] == [0.9, 0.2]
    top = ranked[0]
    assert top.description == "Gateway deploy runbook"
    assert top.tags == ["type/reference", "ops"]
    assert "restarting the gateway" in top.chunk


async def test_rerank_truncates_to_k_and_keeps_vector_order_on_ties(
    tmp_path: Path, catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("a", "b", "c"):
        _write(tmp_path, f"{name}.md", f"---\ntype: Memory\n---\n\nbody {name}")
    _install(monkeypatch, _FakeJudge({"body c": 0.9}, default=0.4))

    ranked = await _rank(tmp_path, catalog, [_hit(tmp_path, f"{n}.md") for n in "abc"], k=2)
    assert [item.path for item in ranked] == ["c.md", "a.md"]


async def test_each_candidate_is_its_own_request_with_its_own_state(
    tmp_path: Path, catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "ops/runbook.md", _NOTE)
    _write(tmp_path, "b.md", "---\ntype: Memory\n---\n\nbody b")
    judge = _FakeJudge()
    _install(monkeypatch, judge)

    await _rank(
        tmp_path,
        catalog,
        [_hit(tmp_path, "ops/runbook.md"), _hit(tmp_path, "b.md")],
        query="the query",
    )

    assert len(judge.calls) == 2
    by_path = {
        ("ops/runbook.md" if "ops/runbook.md" in s else "b.md"): s for _, s, _ in judge.calls
    }
    runbook, other = by_path["ops/runbook.md"], by_path["b.md"]
    # the query comes first, then exactly one candidate note: path, description, excerpt
    assert runbook.index("the query") < runbook.index("path: ops/runbook.md")
    assert "b.md" not in runbook and "ops/runbook.md" not in other
    assert "description: Gateway deploy runbook" in runbook
    assert "description: (none)" in other
    assert "restarting the gateway" in runbook.split("excerpt:\n", 1)[1]
    assert {model for model, _, _ in judge.calls} == {"m"}


async def test_description_row_hit_is_shown_as_its_excerpt(
    tmp_path: Path, catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "ops/runbook.md", _NOTE)
    judge = _FakeJudge()
    _install(monkeypatch, judge)

    await _rank(tmp_path, catalog, [_hit(tmp_path, "ops/runbook.md", KIND_DESC)])

    assert judge.calls[0][1].endswith("excerpt:\nGateway deploy runbook")


def test_excerpt_is_capped_in_the_state() -> None:
    candidate = rerank._Candidate("n.md", "z" * 5000, "d", [])
    state = rerank._pair_state("q", candidate, "search")
    assert state.count("z") == 1800


async def test_purpose_selects_the_state_lead_in_and_the_question(
    tmp_path: Path, catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "a.md", "---\ntype: Memory\n---\n\nbody a")
    seen: dict[str, tuple[str, YesNo]] = {}
    for purpose in ("recall", "search", "dedup"):
        judge = _FakeJudge()
        _install(monkeypatch, judge)
        await _rank(tmp_path, catalog, [_hit(tmp_path, "a.md")], query="Q!", purpose=purpose)
        _, state, question = judge.calls[0]
        seen[purpose] = (state, question)

    leads = {state.splitlines()[0] for state, _ in seen.values()}
    questions = {question for _, question in seen.values()}
    assert len(leads) == len(questions) == 3
    for state, question in seen.values():
        assert "Q!" in state
        assert question.question and question.true_if and question.false_if
    # The recall and search state keeps its calibrated shape; only dedup labels the candidate.
    assert "\n\nCandidate memory note:\n" in seen["recall"][0]
    assert "\n\nCandidate memory note:\n" in seen["search"][0]
    assert "\n\nExisting memory note:\n" in seen["dedup"][0]


async def test_duplicate_check_is_billed_apart_from_search(
    tmp_path: Path, catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "a.md", "---\ntype: Memory\n---\n\nbody a")
    kinds: dict[str, list[str]] = {}
    for purpose in ("recall", "search", "dedup"):
        judge = _FakeJudge()
        _install(monkeypatch, judge)
        await _rank(tmp_path, catalog, [_hit(tmp_path, "a.md")], purpose=purpose)
        kinds[purpose] = judge.usage_kinds
    assert kinds == {
        "recall": ["memory_rerank"],
        "search": ["memory_rerank"],
        "dedup": ["memory_write_dedup"],
    }


async def test_stale_hits_are_skipped(
    tmp_path: Path, catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deleted file, an unreadable file and a chunk the file no longer has are dropped."""
    _write(tmp_path, "live.md", "---\ntype: Memory\n---\n\nlive body")
    _write(tmp_path, "short.md", "---\ntype: Memory\n---\n\nshort body")
    (tmp_path / "binary.md").write_bytes(b"\xff\xfe\x00bad")
    judge = _FakeJudge({}, default=0.5)
    _install(monkeypatch, judge)

    ranked = await _rank(
        tmp_path,
        catalog,
        [
            _hit(tmp_path, "gone.md"),
            _hit(tmp_path, "binary.md"),
            _hit(tmp_path, "short.md", KIND_BODY, 7),
            _hit(tmp_path, "live.md"),
        ],
    )
    assert [item.path for item in ranked] == ["live.md"]
    assert len(judge.calls) == 1


# ── how many notes are ranked ──


def _numbered_notes(root: Path, count: int) -> list[ChunkHit]:
    hits: list[ChunkHit] = []
    for i in range(count):
        _write(root, f"n{i}.md", f"---\ntype: Memory\n---\n\nbody {i}")
        hits.append(_hit(root, f"n{i}.md"))
    return hits


async def test_at_most_limit_candidates_are_ranked_whatever_k_is(
    tmp_path: Path, catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    hits = _numbered_notes(tmp_path, 12)
    judge = _FakeJudge()
    _install(monkeypatch, judge)

    ranked = await _rank(tmp_path, catalog, hits, k=100, limit=5)

    assert len(judge.calls) == 5
    assert [item.path for item in ranked] == [f"n{i}.md" for i in range(5)]


async def test_excluded_notes_are_neither_read_nor_ranked_and_take_no_slot(
    tmp_path: Path, catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    hits = _numbered_notes(tmp_path, 12)
    judge = _FakeJudge()
    _install(monkeypatch, judge)

    ranked = await _rank(
        tmp_path, catalog, hits, k=100, limit=5, exclude=frozenset({"n0.md", "n2.md", "n3.md"})
    )

    assert [item.path for item in ranked] == ["n1.md", "n4.md", "n5.md", "n6.md", "n7.md"]
    assert len(judge.calls) == 5
    assert not any(f"body {i}" in state for i in (0, 2, 3) for _m, state, _q in judge.calls)


async def test_a_stale_hit_does_not_take_one_of_the_limited_slots(
    tmp_path: Path, catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    hits = [_hit(tmp_path, "gone.md"), *_numbered_notes(tmp_path, 4)]
    _install(monkeypatch, _FakeJudge())

    ranked = await _rank(tmp_path, catalog, hits, k=100, limit=2)

    assert [item.path for item in ranked] == ["n0.md", "n1.md"]


async def test_exclusion_matches_the_pool_relative_path_of_an_index_path(
    tmp_path: Path, catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index stores absolute paths; an excluded path is pool-relative."""
    hits = _numbered_notes(tmp_path, 2)
    _write(tmp_path, "sub/n0.md", "---\ntype: Memory\n---\n\nnested")
    _install(monkeypatch, _FakeJudge())

    ranked = await _rank(
        tmp_path, catalog, [*hits, _hit(tmp_path, "sub/n0.md")], exclude=frozenset({"n0.md"})
    )

    assert sorted(item.path for item in ranked) == ["n1.md", "sub/n0.md"]


async def test_cancelling_a_search_cancels_the_requests_still_running(
    tmp_path: Path, catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    hits = _numbered_notes(tmp_path, 8)
    started = asyncio.Event()
    cancelled: list[str] = []

    async def never_answers(_model: str, state: str, *_a: object, **_k: object) -> float:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(state)
            raise
        raise AssertionError("unreachable")

    monkeypatch.setattr(rerank, "ask_relevance", never_answers)
    search = asyncio.create_task(_rank(tmp_path, catalog, hits))
    await asyncio.wait_for(started.wait(), timeout=5)
    await asyncio.sleep(0)

    search.cancel()
    with pytest.raises(asyncio.CancelledError):
        await search

    assert len(cancelled) == 8


def test_description_and_tags_survive_malformed_frontmatter(tmp_path: Path) -> None:
    """A note that says nothing usable about itself surfaces empty metadata, never a crash:
    non-string values coerce, malformed frontmatter is a plain body."""
    _write(tmp_path, "scalar.md", "---\ndescription: 123\ntags: 5\n---\nbody text\n")
    _write(tmp_path, "bad_yaml.md", "---\ntags: [unclosed\n---\nbody text\n")
    _write(tmp_path, "plain.md", "# Just a heading\n")
    hits = [
        _hit(tmp_path, "scalar.md", KIND_DESC),
        *(_hit(tmp_path, n) for n in ("bad_yaml.md", "plain.md")),
    ]

    scalar, bad_yaml, plain = rerank._load_candidates(hits, tmp_path, frozenset(), 30)

    assert (scalar.description, scalar.tags, scalar.chunk) == ("123", [], "123")
    assert (bad_yaml.description, bad_yaml.tags) == ("", [])
    assert (plain.description, plain.tags, plain.chunk) == ("", [], "# Just a heading")


async def test_no_candidates_asks_no_question(
    tmp_path: Path, catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    judge = _FakeJudge()
    _install(monkeypatch, judge)
    assert await _rank(tmp_path, catalog, []) == []
    assert judge.calls == []


# ── concurrency ──


async def test_requests_run_concurrently_up_to_the_cap(
    tmp_path: Path, catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    count = rerank._MAX_CONCURRENT_DECISIONS * 2 + 3
    hits: list[ChunkHit] = []
    for i in range(count):
        _write(tmp_path, f"n{i}.md", f"---\ntype: Memory\n---\n\nbody {i}")
        hits.append(_hit(tmp_path, f"n{i}.md"))
    judge = _FakeJudge()
    _install(monkeypatch, judge)

    ranked = await _rank(tmp_path, catalog, hits, k=count, limit=count)

    assert len(judge.calls) == count
    assert len(ranked) == count
    assert 1 < judge.max_in_flight <= rerank._MAX_CONCURRENT_DECISIONS


def test_default_candidates_all_go_out_together() -> None:
    """The default depth (30, from the calibration benchmark) fits under the concurrency cap, so a
    default search sends every request at once."""
    assert settings.services.memory_rerank_candidates == 30
    assert settings.services.memory_rerank_candidates <= rerank._MAX_CONCURRENT_DECISIONS


# ── failure ──


async def test_a_failed_request_fails_the_search_with_the_cause(
    tmp_path: Path, catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("a", "b", "c"):
        _write(tmp_path, f"{name}.md", f"---\ntype: Memory\n---\n\nbody {name}")

    async def failing(*_args: object, **_kwargs: object) -> object:
        raise DecisionRequestError("jev returned 503")

    monkeypatch.setattr(rerank, "decide", failing)
    with pytest.raises(IndexerUnavailable, match=r"memory rerank failed \(m\).*jev returned 503"):
        await _rank(tmp_path, catalog, [_hit(tmp_path, f"{n}.md") for n in "abc"])


async def test_ask_relevance_reads_the_true_probability_and_clamps(
    catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}
    answers = iter(
        [
            Decision(top=TRUE, probabilities={TRUE: 1.0000000005, FALSE: 0.0}),
            Decision(top=FALSE, probabilities={TRUE: 0.3, FALSE: 0.7}),
        ]
    )

    async def fake_decide(
        model: str, state: str, questions: Mapping[str, YesNo], **kwargs: object
    ) -> dict[str, Decision]:
        seen.update(model=model, state=state, usage_kind=kwargs["usage_kind"], ids=set(questions))
        return {next(iter(questions)): next(answers)}

    monkeypatch.setattr(rerank, "decide", fake_decide)
    question = YesNo("q?", "yes", "no")
    kind = "memory_write_dedup"
    assert (
        await rerank.ask_relevance("m", "the state", question, catalog=catalog, usage_kind=kind)
        == 1.0
    )
    assert (
        await rerank.ask_relevance("m", "the state", question, catalog=catalog, usage_kind=kind)
        == 0.3
    )
    assert seen["model"] == "m"
    assert seen["state"] == "the state"
    assert seen["usage_kind"] == kind
    assert len(seen["ids"]) == 1  # pyright: ignore[reportArgumentType]


async def test_unknown_decision_model_is_a_clear_error(catalog: ModelCatalog) -> None:
    with pytest.raises(IndexerUnavailable, match=r"memory rerank failed.*not-a-decision-model"):
        await rerank.ask_relevance(
            "not-a-decision-model",
            "state",
            YesNo("q?", "yes", "no"),
            catalog=catalog,
            usage_kind="memory_rerank",
        )
