"""The second search stage: restoring chunk text, batching, ranking and failure semantics.

The decision model is replaced by fakes at the two seams `rerank.py` confines it to
(`decision_limits` and `ask_relevance`) and, for the error mapping, at `decide` itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from base.agents import IndexerUnavailable
from base.lm.catalog import ModelCatalog
from base.lm.catalog.decide.types import (
    FALSE,
    TRUE,
    Decision,
    DecisionRequestError,
    YesNo,
    estimate_tokens,
    question_text,
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
    """Stands in for the decision call: probability by a rule over the question text."""

    def __init__(self, rule: Mapping[str, float], default: float = 0.0) -> None:
        self.rule = rule
        self.default = default
        self.calls: list[tuple[str, str, dict[str, YesNo]]] = []

    async def __call__(
        self, model: str, query: str, questions: Mapping[str, YesNo], *, catalog: ModelCatalog
    ) -> dict[str, float]:
        del catalog
        self.calls.append((model, query, dict(questions)))
        return {
            key: next(
                (p for marker, p in self.rule.items() if marker in question.question),
                self.default,
            )
            for key, question in questions.items()
        }


def _roomy_limits(_model: str, _catalog: ModelCatalog) -> rerank.DecisionLimits:
    return rerank.DecisionLimits(32_000, 64_000)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    judge: _FakeJudge,
    *,
    context: int = 32_000,
    request: int = 64_000,
) -> None:
    monkeypatch.setattr(rerank, "ask_relevance", judge)

    def limits(_model: str, _catalog: ModelCatalog) -> rerank.DecisionLimits:
        return rerank.DecisionLimits(context, request)

    monkeypatch.setattr(rerank, "decision_limits", limits)


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

    ranked = await rerank.rerank(
        "how do I restart the gateway",
        # vector order puts the irrelevant note first
        [_hit(tmp_path, "other.md"), _hit(tmp_path, "ops/runbook.md")],
        model="jev-test",
        catalog=catalog,
        k=5,
        memory_root=tmp_path,
    )

    assert [item.path for item in ranked] == ["ops/runbook.md", "other.md"]
    assert [item.score for item in ranked] == [0.9, 0.2]
    top = ranked[0]
    assert top.description == "Gateway deploy runbook"
    assert top.tags == ["type/reference", "ops"]
    assert "restarting the gateway" in top.chunk
    # the query is the state; the model id is passed through
    assert judge.calls[0][:2] == ("jev-test", "how do I restart the gateway")


async def test_rerank_truncates_to_k_and_keeps_vector_order_on_ties(
    tmp_path: Path, catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("a", "b", "c"):
        _write(tmp_path, f"{name}.md", f"---\ntype: Memory\n---\n\nbody {name}")
    _install(monkeypatch, _FakeJudge({"body c": 0.9}, default=0.4))

    ranked = await rerank.rerank(
        "q",
        [_hit(tmp_path, "a.md"), _hit(tmp_path, "b.md"), _hit(tmp_path, "c.md")],
        model="m",
        catalog=catalog,
        k=2,
        memory_root=tmp_path,
    )
    assert [item.path for item in ranked] == ["c.md", "a.md"]


async def test_question_names_the_note_and_carries_the_chunk_text(
    tmp_path: Path, catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "ops/runbook.md", _NOTE)
    judge = _FakeJudge({})
    _install(monkeypatch, judge)

    await rerank.rerank(
        "q",
        [_hit(tmp_path, "ops/runbook.md"), _hit(tmp_path, "ops/runbook.md", KIND_DESC)],
        model="m",
        catalog=catalog,
        k=5,
        memory_root=tmp_path,
    )

    questions = list(judge.calls[0][2].values())
    assert len(questions) == 2
    for question in questions:
        assert "ops/runbook.md" in question.question
        assert "Gateway deploy runbook" in question.question
    assert "restarting the gateway" in questions[0].question
    assert questions[1].question.rstrip().endswith("Gateway deploy runbook")


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

    scalar, bad_yaml, plain = rerank._load_candidates(hits, tmp_path)

    assert (scalar.description, scalar.tags, scalar.chunk) == ("123", [], "123")
    assert (bad_yaml.description, bad_yaml.tags) == ("", [])
    assert (plain.description, plain.tags, plain.chunk) == ("", [], "# Just a heading")


async def test_stale_hits_are_skipped(
    tmp_path: Path, catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deleted file, an unreadable file and a chunk the file no longer has are dropped."""
    _write(tmp_path, "live.md", "---\ntype: Memory\n---\n\nlive body")
    _write(tmp_path, "short.md", "---\ntype: Memory\n---\n\nshort body")
    (tmp_path / "binary.md").write_bytes(b"\xff\xfe\x00bad")
    judge = _FakeJudge({}, default=0.5)
    _install(monkeypatch, judge)

    ranked = await rerank.rerank(
        "q",
        [
            _hit(tmp_path, "gone.md"),
            _hit(tmp_path, "binary.md"),
            _hit(tmp_path, "short.md", KIND_BODY, 7),
            _hit(tmp_path, "live.md"),
        ],
        model="m",
        catalog=catalog,
        k=5,
        memory_root=tmp_path,
    )
    assert [item.path for item in ranked] == ["live.md"]
    assert len(judge.calls[0][2]) == 1


async def test_no_candidates_asks_no_question(
    tmp_path: Path, catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    judge = _FakeJudge({})
    _install(monkeypatch, judge)
    assert await rerank.rerank("q", [], model="m", catalog=catalog, k=5, memory_root=tmp_path) == []
    assert judge.calls == []


# ── batching ──


async def test_questions_are_batched_to_fit_the_request_limit(
    tmp_path: Path, catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    hits: list[ChunkHit] = []
    for i in range(6):
        _write(tmp_path, f"n{i}.md", f"---\ntype: Memory\n---\n\nbody {i} " + "word " * 100)
        hits.append(_hit(tmp_path, f"n{i}.md"))
    judge = _FakeJudge({f"body {i}": i / 10 for i in range(6)})
    query = "a query"
    one_question = estimate_tokens(
        question_text(rerank._question_of(rerank._load_candidates(hits[:1], tmp_path)[0]))
    )
    # room for the query plus two questions per request
    limit = estimate_tokens(query) + 2 * one_question + 5
    _install(monkeypatch, judge, context=limit, request=limit)

    ranked = await rerank.rerank(query, hits, model="m", catalog=catalog, k=6, memory_root=tmp_path)

    assert [len(call[2]) for call in judge.calls] == [2, 2, 2]
    for _model, _query, batch in judge.calls:
        used = estimate_tokens(query) + sum(
            estimate_tokens(question_text(q)) for q in batch.values()
        )
        assert used <= limit
    # every candidate is scored once and the ranking spans the batches
    assert [item.path for item in ranked] == [f"n{i}.md" for i in (5, 4, 3, 2, 1, 0)]


async def test_one_oversized_question_gets_its_own_request(
    tmp_path: Path, catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision call, not the batcher, rejects what cannot fit alone: nothing is shortened."""
    for name in ("a", "b"):
        _write(tmp_path, f"{name}.md", f"---\ntype: Memory\n---\n\nbody {name}")
    judge = _FakeJudge({})
    _install(monkeypatch, judge, context=1, request=1)

    await rerank.rerank(
        "q",
        [_hit(tmp_path, "a.md"), _hit(tmp_path, "b.md")],
        model="m",
        catalog=catalog,
        k=5,
        memory_root=tmp_path,
    )
    assert [len(call[2]) for call in judge.calls] == [1, 1]


# ── failure ──


async def test_a_failed_batch_fails_the_search_with_the_cause(
    tmp_path: Path, catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "a.md", "---\ntype: Memory\n---\n\nbody")

    async def failing(*_args: object, **_kwargs: object) -> object:
        raise DecisionRequestError("jev returned 503")

    monkeypatch.setattr(rerank, "decide", failing)
    monkeypatch.setattr(rerank, "decision_limits", _roomy_limits)
    with pytest.raises(IndexerUnavailable, match=r"memory rerank failed \(m\).*jev returned 503"):
        await rerank.rerank(
            "q", [_hit(tmp_path, "a.md")], model="m", catalog=catalog, k=5, memory_root=tmp_path
        )


async def test_ask_relevance_reads_the_true_probability_and_clamps(
    catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    async def fake_decide(
        model: str, state: str, questions: Mapping[str, YesNo], **kwargs: object
    ) -> dict[str, Decision]:
        seen.update(model=model, state=state, usage_kind=kwargs["usage_kind"])
        return {
            "0": Decision(top=TRUE, probabilities={TRUE: 1.0000000005, FALSE: 0.0}),
            "1": Decision(top=FALSE, probabilities={TRUE: 0.3, FALSE: 0.7}),
        }

    monkeypatch.setattr(rerank, "decide", fake_decide)
    question = YesNo("q?", "yes", "no")
    result = await rerank.ask_relevance(
        "m", "the query", {"0": question, "1": question}, catalog=catalog
    )
    assert result == {"0": 1.0, "1": 0.3}
    assert seen == {"model": "m", "state": "the query", "usage_kind": rerank.USAGE_KIND}


def test_unknown_decision_model_is_a_clear_error(catalog: ModelCatalog) -> None:
    with pytest.raises(IndexerUnavailable, match="not a known decision model"):
        rerank.decision_limits("gpt-not-a-decision-model", catalog)
