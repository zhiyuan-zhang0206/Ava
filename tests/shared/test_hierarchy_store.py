"""`shared.agents.history.hierarchy.store` — the understanding-tree table contract.

Exercised against the session's real Postgres: the contract IS the upsert —
one row per (agent, depth, span) identity, identical text is a no-op rewrite,
a regenerated text overwrites in place; parent links resolve from children
spans; the input-hash cache feeds the generation reuse path; the window read
returns exactly the intersecting nodes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from shared.agents.history.hierarchy.generate import input_hash, text_hash
from shared.agents.history.hierarchy.pipeline import MaterializedNode
from shared.agents.history.hierarchy.store import load_known_texts, load_window_nodes, write_tree

AGENT_A = 990_128_901  # round-trip test
AGENT_B = 990_128_902  # idempotence test
AGENT_C = 990_128_903  # reuse-cache test
AGENT_D = 990_128_904  # window-filter test
AGENT_E = 990_128_905  # re-cut reconciliation test
AGENT_F = 990_128_906  # pending stretch across a compact-only pass
AGENT_G = 990_128_907  # same-version replay identity

# 2026-09-12 12:00-12:05 Beijing == 04:00-04:05 UTC.
TS0, TS1 = "2026-09-12T12:00:00+08:00", "2026-09-12T12:05:00+08:00"
T0 = datetime(2026, 9, 12, 4, 0, tzinfo=UTC)
T1 = datetime(2026, 9, 12, 6, 0, tzinfo=UTC)


def node(
    nid: str,
    *,
    level: int = 1,
    span: tuple[int, int] = (0, 5),
    at: tuple[str, str] = (TS0, TS1),
    text: str = "narrative text",
    kind: str = "group",
    trigger: str = "compact@i9",
    children: tuple[str, ...] = (),
    children_spans: tuple[tuple[int, int], ...] = (),
    input_text: str = "material",
) -> MaterializedNode:
    return MaterializedNode(
        nid=nid,
        level=level,
        kind=kind,
        span=span,
        at=at,
        trigger=trigger,
        src_tok=100,
        text=text,
        text_hash=text_hash(text),
        input_hash=input_hash("leaf" if level == 1 else "node", input_text),
        children=children,
        children_spans=children_spans,
    )


def test_write_tree_round_trip_and_parent_link() -> None:
    leaf = node("L1#1", level=1, span=(0, 4), text="leaf text", input_text="blocks...")
    parent = node(
        "L2#1",
        level=2,
        span=(0, 9),
        text="parent text",
        input_text="leaf text",
        children=("L1#1",),
        children_spans=((0, 4),),
    )
    assert write_tree(AGENT_A, [leaf, parent], model="deepseek-v4-flash") == 2

    rows = load_window_nodes(AGENT_A, T0, T1)
    by_depth = {r.depth: r for r in rows}
    assert set(by_depth) == {1, 2}
    assert (by_depth[1].span_start, by_depth[1].span_end) == (0, 4)
    assert by_depth[1].text == "leaf text"
    assert by_depth[2].span_end == 9
    assert by_depth[1].parent_id == by_depth[2].id
    assert by_depth[2].parent_id is None
    assert by_depth[1].start_ts == T0


def test_write_tree_is_idempotent_and_regenerates_in_place() -> None:
    first = node("L1#1", span=(0, 4), text="v1", input_text="m1")
    write_tree(AGENT_B, [first], model="m")
    write_tree(AGENT_B, [first], model="m")  # identical rewrite: no new row
    rows = load_window_nodes(AGENT_B, T0, T1)
    assert len(rows) == 1 and rows[0].text == "v1"

    regenerated = node("L1#1", span=(0, 4), text="v2", input_text="m2")
    write_tree(AGENT_B, [regenerated], model="m")
    rows = load_window_nodes(AGENT_B, T0, T1)
    assert len(rows) == 1 and rows[0].text == "v2"


def test_load_known_texts_feeds_the_reuse_cache() -> None:
    a = node("L1#1", span=(0, 4), text="alpha", input_text="input-a")
    b = node("L1#2", span=(5, 9), text="beta", input_text="input-b")
    write_tree(AGENT_C, [a, b], model="m")
    known = load_known_texts(AGENT_C)
    assert known[a.input_hash] == "alpha"
    assert known[b.input_hash] == "beta"


def test_window_filter_returns_only_intersecting_nodes() -> None:
    early = node("L1#1", span=(0, 4), at=(TS0, TS1), text="early", input_text="e")
    late = node(
        "L1#2",
        span=(5, 9),
        at=("2026-09-12T13:00:00+08:00", "2026-09-12T13:10:00+08:00"),
        text="late",
        input_text="l",
    )
    write_tree(AGENT_D, [early, late], model="m")
    window = load_window_nodes(AGENT_D, T0, datetime(2026, 9, 12, 4, 30, tzinfo=UTC))
    assert [r.text for r in window] == ["early"]
    wide = load_window_nodes(AGENT_D, T0, T1)
    assert {r.text for r in wide} == {"early", "late"}


def test_recut_tail_replaces_the_superseded_cut() -> None:
    """A rebuild after history grew re-cuts the tail: the earlier cut's row is
    reconciled away instead of standing beside the new one."""
    stable = node("L1#0", span=(0, 30), text="stable", input_text="blocks 0..30")
    first_cut = node("L1#1", span=(31, 40), text="tail v1", input_text="stretch 31..40")
    write_tree(AGENT_E, [stable, first_cut], model="m")

    recut = node("L1#1", span=(31, 47), text="tail v2", input_text="stretch 31..47")
    write_tree(AGENT_E, [stable, recut], model="m")

    spans = {(r.depth, r.span_start, r.span_end) for r in load_window_nodes(AGENT_E, T0, T1)}
    assert spans == {(1, 0, 30), (1, 31, 47)}


def test_unreproduced_rows_outside_the_recut_survive() -> None:
    """A compact-driven pass seals no tail: rows of a stretch left pending stay —
    they are still that region's best coverage."""
    stable = node("L1#0", span=(0, 30), text="stable", input_text="blocks 0..30")
    pending = node("L1#1", span=(31, 40), text="tail", input_text="stretch 31..40")
    write_tree(AGENT_F, [stable, pending], model="m")

    write_tree(AGENT_F, [stable], model="m")

    spans = {(r.depth, r.span_start, r.span_end) for r in load_window_nodes(AGENT_F, T0, T1)}
    assert spans == {(1, 0, 30), (1, 31, 40)}


def test_same_version_replay_reproduces_sealed_rows_identically() -> None:
    """The append-only anchor: a rebuild over unchanged history reproduces the
    sealed rows identically — same ids, same contents (only updated_at ticks)."""
    nodes = [
        node("L1#0", span=(0, 30), text="a", input_text="i0"),
        node("L1#1", span=(31, 40), text="b", input_text="i1"),
    ]
    write_tree(AGENT_G, nodes, model="m")
    first = {
        (r.id, r.depth, r.span_start, r.span_end, r.text, r.parent_id)
        for r in load_window_nodes(AGENT_G, T0, T1)
    }

    write_tree(AGENT_G, nodes, model="m")
    second = {
        (r.id, r.depth, r.span_start, r.span_end, r.text, r.parent_id)
        for r in load_window_nodes(AGENT_G, T0, T1)
    }

    assert second == first
