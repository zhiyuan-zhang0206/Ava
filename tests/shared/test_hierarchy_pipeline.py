"""Unit tests for the hierarchy pipeline (`shared/hierarchy/pipeline.py`).

Contracts locked here: trigger batches cut at compact items (tail optional);
units carry rendered sizes and stable uid/spans; materialize walks levels and
feeds children texts upward; the input-hash cache skips model calls for known
inputs; a failed child blocks its parent without a call; aliases copy their
child's text. Model calls go through a deterministic fake — no network.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from shared.hierarchy import pipeline as pipeline_module
from shared.hierarchy.blocks import fold_blocks
from shared.hierarchy.generate import input_hash, text_hash
from shared.hierarchy.pipeline import (
    MaterializedTree,
    build_agent_tree,
    build_units,
    materialize,
    trigger_batches,
)
from shared.hierarchy.seal import (
    NodeSpec,
    SealResult,
    Unit,
    narrative_budget_tok,
    seal_cascade,
)
from shared.hierarchy.tokens import count_tokens
from shared.timeline import build_timeline_items

MODEL = "deepseek-v4-flash"
T0 = "2026-09-12T12:00:00+08:00"


def inbound(text: str, ts: str = T0) -> HumanMessage:
    return HumanMessage(
        content=text, additional_kwargs={"ava_msg_type": "inbound", "ava_created_at": ts}
    )


def ai_msg(text: str, ts: str = T0) -> AIMessage:
    return AIMessage(content=text, additional_kwargs={"ava_created_at": ts})


def exec_out(text: str, ts: str = T0) -> ToolMessage:
    return ToolMessage(
        content=text,
        tool_call_id="tc1",
        additional_kwargs={"ava_msg_type": "exec_output", "ava_exit_code": 0, "ava_created_at": ts},
    )


def compact(ts: str = T0) -> HumanMessage:
    return HumanMessage(
        content="[system] compacted",
        additional_kwargs={"ava_msg_type": "compact_summary", "ava_created_at": ts},
    )


class FakeLLM:
    def __init__(self, responder: Callable[[list[BaseMessage]], str | Exception]) -> None:
        self.responder = responder
        self.calls: list[list[BaseMessage]] = []
        self._lock = threading.Lock()

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        with self._lock:
            self.calls.append(messages)
        time.sleep(0.005)
        content = self.responder(messages)
        if isinstance(content, Exception):
            raise content
        return AIMessage(content=content)


def call_material(call: list[BaseMessage]) -> str:
    content = call[0].content
    assert isinstance(content, list)
    return str(cast("dict[str, Any]", content[0])["text"])


def render_all(msgs: Sequence[BaseMessage]):
    items, _ = build_timeline_items(msgs, [])
    blocks = fold_blocks(items)
    return items, blocks, build_units(msgs, blocks)


# ---- trigger batches ----


def test_trigger_batches_cut_at_compact_items() -> None:
    msgs: list[BaseMessage] = [
        inbound("m0"),
        ai_msg("m1"),
        ai_msg("m2"),
        compact(),
        ai_msg("m3"),
        inbound("m4"),
        ai_msg("m5"),
    ]
    items, _blocks, (units, table) = render_all(msgs)
    assert [u.span for u in units] == [(0, 0), (1, 1), (2, 2), (4, 4), (5, 5), (6, 6)]

    batches = trigger_batches(items, units, include_tail=False)
    assert [b.name for b in batches] == ["compact@i3"]
    assert [u.uid for u in batches[0].units] == ["b0", "b1", "b2"]

    batches = trigger_batches(items, units, include_tail=True)
    assert [b.name for b in batches] == ["compact@i3", "tail"]
    assert [u.uid for u in batches[1].units] == ["b4", "b5", "b6"]
    assert set(table) == {u.uid for u in units}


def test_build_units_carries_render_size_and_times() -> None:
    # One AI message plus the tool result it answers fold into one block (0..1).
    msgs = [ai_msg("working"), exec_out("Code execution output: done")]
    _items, _blocks, (units, table) = render_all(msgs)
    (u0,) = units
    assert u0.uid == "b0"
    assert u0.tok > 0
    assert u0.at == (T0, T0)
    assert u0.span == (0, 1)
    assert table["b0"].i0 == 0 and table["b0"].i1 == 1


# ---- materialize: happy path, cache, failures, aliases ----


def _six_block_tree(msgs: Sequence[BaseMessage]):
    _items, _blocks, (units, table) = render_all(msgs)
    sealed = seal_cascade([("t1", list(units))])
    return sealed, table


def test_materialize_generates_a_leaf_group() -> None:
    msgs = [inbound(f"m{i}") for i in range(6)]
    sealed, table = _six_block_tree(msgs)
    assert [s.nid for s in sealed.nodes] == ["L1#1"]

    fake = FakeLLM(lambda _m: "summary text")
    tree = materialize(msgs, sealed, table, llm=fake, model=MODEL, retry_attempts=0)
    (node,) = tree.nodes
    assert node.nid == "L1#1" and node.level == 1 and node.kind == "group"
    assert node.text == "summary text"
    assert node.span == (0, 5)
    assert node.children == ("b0", "b1", "b2", "b3", "b4", "b5")
    assert node.text_hash == text_hash("summary text")
    assert tree.errors == ()
    # the call carried the assembled leaf input and reported the right key
    assert len(fake.calls) == 1
    material = call_material(fake.calls[0])
    assert material.startswith("# node L1#1 - 6 source blocks")
    assert "### block 0 (messages i0-i0" in material
    assert node.input_hash == input_hash("leaf", material)


def test_materialize_reuses_known_input_hash_without_a_call() -> None:
    msgs = [inbound(f"m{i}") for i in range(6)]
    sealed, table = _six_block_tree(msgs)
    fake = FakeLLM(lambda _m: "fresh")
    first = materialize(msgs, sealed, table, llm=fake, model=MODEL, retry_attempts=0)
    (node,) = first.nodes

    fake2 = FakeLLM(lambda _m: "should not be called")
    second = materialize(
        msgs,
        sealed,
        table,
        llm=fake2,
        model=MODEL,
        retry_attempts=0,
        known_texts={node.input_hash: "cached text"},
    )
    (node2,) = second.nodes
    assert node2.text == "cached text"
    assert node2.input_hash == node.input_hash
    assert fake2.calls == []


def test_failed_child_blocks_the_parent_without_a_call() -> None:
    msgs = [inbound(f"m{i}") for i in range(10)]
    _items, _blocks, (units, table) = render_all(msgs)
    l1a = NodeSpec(
        nid="L1#1",
        level=1,
        units=tuple(units[:5]),
        kind="group",
        src_tok=500,
        span=(0, 4),
        at=(T0, T0),
        trigger="t1",
    )
    l1b = NodeSpec(
        nid="L1#2",
        level=1,
        units=tuple(units[5:]),
        kind="group",
        src_tok=500,
        span=(5, 9),
        at=(T0, T0),
        trigger="t1",
    )
    l2 = NodeSpec(
        nid="L2#1",
        level=2,
        units=(
            Unit(uid="L1#1", tok=50, span=(0, 4), at=(T0, T0), kind="node"),
            Unit(uid="L1#2", tok=50, span=(5, 9), at=(T0, T0), kind="node"),
        ),
        kind="group",
        src_tok=100,
        span=(0, 9),
        at=(T0, T0),
        trigger="t2",
    )
    sealed = SealResult(nodes=(l1a, l1b, l2), pending={}, max_level=2)

    def responder(messages: list[BaseMessage]) -> str | Exception:
        return RuntimeError("boom") if "L1#2" in call_material(messages) else "ok"

    fake = FakeLLM(responder)
    tree = materialize(msgs, sealed, table, llm=fake, model=MODEL, retry_attempts=0)
    assert [n.nid for n in tree.nodes] == ["L1#1"]
    assert len(fake.calls) == 2  # L1#1 + L1#2; the parent never called
    (err,) = [e for e in tree.errors if e.nid == "L2#1"]
    assert err.error is not None and "L1#2" in err.error
    assert any(e.nid == "L1#2" and e.error is not None for e in tree.errors)


def test_alias_copies_its_child_text() -> None:
    msgs = [inbound(f"m{i}") for i in range(6)]
    _items, _blocks, (units, table) = render_all(msgs)
    l1 = NodeSpec(
        nid="L1#1",
        level=1,
        units=tuple(units),
        kind="group",
        src_tok=500,
        span=(0, 5),
        at=(T0, T0),
        trigger="t1",
    )
    alias = NodeSpec(
        nid="L2#1",
        level=2,
        units=(Unit(uid="L1#1", tok=50, span=(0, 5), at=(T0, T0), kind="node"),),
        kind="alias",
        src_tok=500,
        span=(0, 5),
        at=(T0, T0),
        trigger="t2",
    )
    sealed = SealResult(nodes=(l1, alias), pending={}, max_level=2)
    fake = FakeLLM(lambda _m: "leaf text")
    tree = materialize(msgs, sealed, table, llm=fake, model=MODEL, retry_attempts=0)
    assert len(fake.calls) == 1
    by_nid = {n.nid: n for n in tree.nodes}
    assert by_nid["L2#1"].kind == "alias"
    assert by_nid["L2#1"].text == "leaf text"
    assert by_nid["L2#1"].children == ("L1#1",)


def test_input_hash_is_deterministic_and_kind_sensitive() -> None:
    assert input_hash("leaf", "x") == input_hash("leaf", "x")
    assert input_hash("leaf", "x") != input_hash("node", "x")
    assert input_hash("leaf", "x") != input_hash("leaf", "y")


# ---- full build (build_agent_tree): end-to-end structure determinism ----


def _fitting_responder(call: list[BaseMessage]) -> str:
    """Deterministic text sized under the call's own budget (structure only)."""
    budget = narrative_budget_tok(count_tokens(call_material(call)))
    text = "\u5b57" * max(budget // 2, 1)
    while text and count_tokens(text) > budget:
        text = text[:-1]
    return text


def test_build_agent_tree_same_input_same_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """The acceptance anchor: one re-run over the same input reproduces the tree.

    The partition (block fold + seal cascade) must be a pure function of the
    message stream; the fixture is sized so the cascade grows a second level
    (100 small units -> 5 level-1 groups -> 1 level-2 group), and every node's
    deterministic fake text fits its budget, so a clean build is expected.
    """
    msgs: list[BaseMessage] = [inbound(f"step {i}") for i in range(100)]

    def fake_loader(agent_id: int) -> list[BaseMessage]:
        return list(msgs)

    monkeypatch.setattr(pipeline_module, "load_checkpoint_messages_full", fake_loader)

    first = build_agent_tree(7, llm=FakeLLM(_fitting_responder), model=MODEL)
    second = build_agent_tree(7, llm=FakeLLM(_fitting_responder), model=MODEL)

    def shape(tree: MaterializedTree) -> list[tuple[int, tuple[int, int], tuple[str, ...], str]]:
        return [(n.level, n.span, n.children, n.kind) for n in tree.nodes]

    assert first.errors == ()
    assert first.max_level >= 2  # the fixture must exercise the cascade
    assert shape(first) == shape(second)
    assert [n.text for n in first.nodes] == [n.text for n in second.nodes]


def test_growth_replays_sealed_batches_and_recuts_only_the_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write-side reconciliation's premise: a rebuild after history grew
    reproduces every compact-sealed cell and re-cuts only the tail span."""
    msgs: list[BaseMessage] = [inbound(f"step {i}") for i in range(30)]
    msgs.append(compact())
    msgs.extend(inbound(f"tail {i}") for i in range(10))

    def fake_loader(agent_id: int) -> list[BaseMessage]:
        return list(msgs)

    monkeypatch.setattr(pipeline_module, "load_checkpoint_messages_full", fake_loader)

    first = build_agent_tree(7, llm=FakeLLM(_fitting_responder), model=MODEL)
    msgs.extend(inbound(f"tail {i}") for i in range(10, 17))
    second = build_agent_tree(7, llm=FakeLLM(_fitting_responder), model=MODEL)

    assert first.errors == () and second.errors == ()
    spans1 = {node.span for node in first.nodes if node.level == 1}
    spans2 = {node.span for node in second.nodes if node.level == 1}
    assert spans1 == {(0, 14), (15, 29), (31, 40)}
    assert spans2 == {(0, 14), (15, 29), (31, 47)}


def test_tail_seal_swap_converges_and_a_second_seal_generates_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The C-leg swap/idempotency contract (review 3187): a tail-sealing pass
    over a history a compact pass already cut produces the same tree as one
    direct tail-sealing pass; sealing the same history again reuses every
    cell's input — zero model calls."""
    msgs: list[BaseMessage] = [inbound(f"step {i}") for i in range(30)]
    msgs.append(compact())
    msgs.extend(inbound(f"tail {i}") for i in range(10))

    def fake_loader(agent_id: int) -> list[BaseMessage]:
        return list(msgs)

    monkeypatch.setattr(pipeline_module, "load_checkpoint_messages_full", fake_loader)

    # Path 1: the compact-driven pass (tail pending), then the tail pass.
    compact_pass = build_agent_tree(
        7, llm=FakeLLM(_fitting_responder), model=MODEL, include_tail=False
    )
    tail_pass = build_agent_tree(7, llm=FakeLLM(_fitting_responder), model=MODEL)
    # Path 2: one tail-sealing pass over the same history.
    direct = build_agent_tree(7, llm=FakeLLM(_fitting_responder), model=MODEL)

    def shape(tree: MaterializedTree) -> list[tuple[int, tuple[int, int], tuple[str, ...], str]]:
        # Structural comparison: the all-reuse path emits the same cells in a
        # different completion order than the generation path.
        return sorted((n.level, n.span, n.children, n.kind) for n in tree.nodes)

    assert compact_pass.errors == () and tail_pass.errors == () and direct.errors == ()
    assert shape(tail_pass) == shape(direct)

    known = {node.input_hash: node.text for node in tail_pass.nodes}
    fake2 = FakeLLM(lambda _m: "should not be called")
    again = build_agent_tree(7, llm=fake2, model=MODEL, known_texts=known)
    assert fake2.calls == []
    assert again.generated == 0 and again.reused == len(tail_pass.nodes)
    assert shape(again) == shape(tail_pass)


def test_growth_after_a_tail_seal_replays_compacts_and_recuts_the_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A compact pass over a grown history replays every compact-sealed cell
    and leaves the tail pending; the tail pass then re-cuts only the tail —
    the same cells a direct run produces."""
    msgs: list[BaseMessage] = [inbound(f"step {i}") for i in range(30)]
    msgs.append(compact())
    msgs.extend(inbound(f"tail {i}") for i in range(10))

    def fake_loader(agent_id: int) -> list[BaseMessage]:
        return list(msgs)

    monkeypatch.setattr(pipeline_module, "load_checkpoint_messages_full", fake_loader)

    first = build_agent_tree(7, llm=FakeLLM(_fitting_responder), model=MODEL)
    msgs.extend(inbound(f"tail {i}") for i in range(10, 17))

    compact_only = build_agent_tree(
        7, llm=FakeLLM(_fitting_responder), model=MODEL, include_tail=False
    )
    second = build_agent_tree(7, llm=FakeLLM(_fitting_responder), model=MODEL)

    assert first.errors == () and compact_only.errors == () and second.errors == ()
    spans1 = {node.span for node in first.nodes if node.level == 1}
    spans_compact_only = {node.span for node in compact_only.nodes if node.level == 1}
    spans2 = {node.span for node in second.nodes if node.level == 1}
    assert spans1 == {(0, 14), (15, 29), (31, 40)}
    # The compact cells replay identically; the grown tail stays pending until
    # the tail pass re-cuts it.
    assert spans_compact_only == {(0, 14), (15, 29)}
    assert spans2 == {(0, 14), (15, 29), (31, 47)}


# ---- generation budget + ordering (task #3704 P2b) ----


def _leaf_group(nid: str, units: Sequence[Unit], span: tuple[int, int]) -> NodeSpec:
    """A hand-built level-1 group over `units` (the budget tests' fixture)."""
    return NodeSpec(
        nid=nid,
        level=1,
        units=tuple(units),
        kind="group",
        src_tok=sum(u.tok for u in units),
        span=span,
        at=(T0, T0),
        trigger="t1",
    )


def _group_fixture(count: int) -> tuple[list[BaseMessage], SealResult, dict[str, Any]]:
    """`count` level-1 groups of 4 units each, over fresh inbound messages."""
    msgs: list[BaseMessage] = [inbound(f"m{i}") for i in range(count * 4)]
    _items, _blocks, (units, table) = render_all(msgs)
    groups = [
        _leaf_group(f"L1#{i + 1}", units[i * 4 : (i + 1) * 4], (i * 4, i * 4 + 3))
        for i in range(count)
    ]
    return msgs, SealResult(nodes=tuple(groups), pending={}, max_level=1), table


def test_deadline_already_passed_skips_everything_without_a_call() -> None:
    """A budget that is already spent stops before the first chunk: nothing is
    attempted, everything is `skipped`, and no model call happens."""
    msgs, sealed, table = _group_fixture(3)
    fake = FakeLLM(lambda _m: "text")
    tree = materialize(
        msgs,
        sealed,
        table,
        llm=fake,
        model=MODEL,
        retry_attempts=0,
        deadline=time.monotonic() - 1,
    )
    assert tree.nodes == ()
    assert tree.errors == ()
    assert tree.skipped == 3
    assert tree.generated == 0 and tree.reused == 0
    assert fake.calls == []


def test_deadline_stops_between_chunks_and_continuation_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The slicing contract: a deadline stops generation between chunks only,
    the stopped nodes stay `skipped`, and a continuation run — same tree, the
    produced texts offered as known — generates exactly the remainder."""
    msgs, sealed, table = _group_fixture(5)
    # max_concurrent=1 -> a chunk is 4 calls; the deadline read after the first
    # chunk (second read) is past it, so exactly one node stays unattempted.
    ticks = iter([0.0, 100.0])
    monkeypatch.setattr(pipeline_module.time, "monotonic", lambda: next(ticks, 100.0))
    fake = FakeLLM(lambda _m: "first pass")
    first = materialize(
        msgs,
        sealed,
        table,
        llm=fake,
        model=MODEL,
        retry_attempts=0,
        max_concurrent=1,
        deadline=50.0,
    )
    assert len(fake.calls) == 4
    assert first.skipped == 1
    assert first.generated == 4

    known = {node.input_hash: node.text for node in first.nodes}
    fake2 = FakeLLM(lambda _m: "continuation")
    second = materialize(
        msgs,
        sealed,
        table,
        llm=fake2,
        model=MODEL,
        retry_attempts=0,
        max_concurrent=1,
        known_texts=known,
    )
    assert second.skipped == 0
    assert len(fake2.calls) == 1  # exactly the skipped node — nothing redone
    assert len(second.nodes) == 5


def test_generation_is_newest_first_within_a_level() -> None:
    """Within a level nodes are independent, so they generate newest-span
    first — the priority a budget-truncated run leaves its coverage on."""
    msgs, sealed, table = _group_fixture(5)
    fake = FakeLLM(lambda _m: "text")
    tree = materialize(
        msgs, sealed, table, llm=fake, model=MODEL, retry_attempts=0, max_concurrent=1
    )
    assert tree.skipped == 0 and len(tree.nodes) == 5
    spans: list[tuple[int, int]] = []
    for call in fake.calls:
        head = call_material(call).splitlines()[0]
        match = re.search(r"i(\d+)-i(\d+)", head)
        assert match is not None
        spans.append((int(match.group(1)), int(match.group(2))))
    assert spans == [(16, 19), (12, 15), (8, 11), (4, 7), (0, 3)]
