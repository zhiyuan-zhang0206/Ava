"""Contract tests for the hierarchy seal partition (`shared/agents/history/hierarchy/seal.py`).

The load-bearing invariants (user-confirmed 2026-09-14, productized under
task #3704): fan-in stays within kappa = [5,15] (extended to 20 only when
every unit is small); a sub-kappa tail carries to the next trigger; sealing
is append-only and deterministic; a group of one aliases instead of
generating a summary. These tests lock the partition contract — the
generation pass (budget/ratio checks) is tested where generation lives.
"""

from __future__ import annotations

from shared.agents.history.hierarchy.seal import (
    NARRATIVE_CAP_TOK,
    NodeSpec,
    SealParams,
    Unit,
    seal_cascade,
    split_units,
)


def unit(i: int, tok: int = 1_000) -> Unit:
    return Unit(uid=f"b{i}", tok=tok, span=(i, i))


def test_carry_below_min_count() -> None:
    """A batch smaller than kappa yields no group; the whole batch carries."""
    groups, carry = split_units([unit(0), unit(1), unit(2), unit(3)])
    assert groups == []
    assert len(carry) == 4


def test_single_group_within_cap() -> None:
    """kappa..cap units seal as one group."""
    groups, carry = split_units([unit(i) for i in range(15)])
    assert [len(g) for g in groups] == [15]
    assert carry == []


def test_even_division_above_cap() -> None:
    """Above the cap the fan-in splits as evenly as possible, in order.

    `tok=2000` keeps the batch out of the all-small extension, pinning the
    standard cap (15).
    """
    groups, carry = split_units([unit(i, tok=2_000) for i in range(16)])
    assert [len(g) for g in groups] == [8, 8]
    assert carry == []
    assert [g[0].uid for g in groups] == ["b0", "b8"]


def test_small_units_extend_to_small_cap() -> None:
    """All-small batches extend the cap to 20; a mixed batch does not."""
    groups, _ = split_units([unit(i, tok=100) for i in range(18)])
    assert [len(g) for g in groups] == [18]
    groups, _ = split_units([unit(0, tok=100_000)] + [unit(i, tok=100) for i in range(17)])
    assert [len(g) for g in groups] == [9, 9]


def test_source_cap_splits_group() -> None:
    """The per-group source guard re-splits a group by token weight."""
    groups, _ = split_units([unit(i, tok=20_000) for i in range(12)])
    assert [len(g) for g in groups] == [7, 5]


def test_oversized_unit_aliases() -> None:
    """A single oversized unit forms its own chunk; the group behind it stands."""
    units = [unit(0, tok=200_000)] + [unit(i) for i in range(1, 6)]
    groups, _ = split_units(units)
    assert [len(g) for g in groups] == [1, 5]
    result = seal_cascade([("t", units)])
    assert [(n.kind, n.level) for n in result.nodes] == [("alias", 1), ("group", 1)]
    alias = result.nodes[0]
    assert alias.units == (units[0],)
    assert alias.nid == "L1#1" and result.nodes[1].nid == "L1#2"


def test_cascade_carries_across_triggers() -> None:
    """Sub-kappa tails wait: 8 -> L1#1, +4 carries, +6 -> L1#2 (carry stays)."""
    result = seal_cascade(
        [
            ("t1", [unit(i) for i in range(8)]),
            ("t2", [unit(i) for i in range(8, 12)]),
            ("t3", [unit(i) for i in range(12, 18)]),
        ]
    )
    assert [n.nid for n in result.nodes] == ["L1#1", "L1#2"]
    assert 0 not in result.pending  # the 10-unit batch consumed the carry
    assert [u.uid for u in result.pending[1]] == ["L1#1", "L1#2"]
    assert result.max_level == 1


def test_cascade_emerges_second_level() -> None:
    """Enough leaves seal a second level: 80 units -> 6 L1 groups -> 1 L2.

    `tok=2000` pins the standard cap (15); under the all-small extension the same
    batch would seal as 4 groups of 20 and stop at level 1.
    """
    result = seal_cascade([("t", [unit(i, tok=2_000) for i in range(80)])])
    levels = [n.level for n in result.nodes]
    assert levels.count(1) == 6 and levels.count(2) == 1
    l2 = result.nodes[-1]
    assert l2.nid == "L2#1" and len(l2.units) == 6
    assert result.max_level == 2
    # Spans cover the batch: first leaf starts at 0, last L1 ends at 79.
    assert result.nodes[0].span == (0, 13)
    assert l2.span == (0, 79)


def test_deterministic() -> None:
    """Same input -> identical result (structure is a pure function)."""
    batch = [unit(i, tok=(i % 3) * 1_000 + 500) for i in range(40)]
    first = seal_cascade([("t1", batch[:20]), ("t2", batch[20:])])
    second = seal_cascade([("t1", batch[:20]), ("t2", batch[20:])])
    assert first == second


def test_budget_is_tenth_capped() -> None:
    """The narrative budget is min(source/10, hard cap) tokens."""
    node = NodeSpec(nid="L1#1", level=1, units=(unit(0), unit(1)), src_tok=40_000)
    assert node.budget_tok() == 4_000
    big = NodeSpec(nid="L1#2", level=1, units=(unit(0), unit(1)), src_tok=1_000_000)
    assert big.budget_tok() == NARRATIVE_CAP_TOK


def test_params_override() -> None:
    """Params are the single tuning point (min_count=3 changes the carry)."""
    p = SealParams(min_count=3)
    groups, carry = split_units([unit(i) for i in range(4)], p)
    assert [len(g) for g in groups] == [4] and carry == []
    groups, carry = split_units([unit(0), unit(1)], p)
    assert groups == [] and len(carry) == 2
