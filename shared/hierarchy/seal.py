"""Deterministic seal/merge for the hierarchical understanding tree.

Contract (user-confirmed 2026-09-14; productized under task #3704):
- Units fold in batches of [5,15] (the cap extends to 20 when every unit is
  "small"); the same rule applies at every level, so depth emerges with
  volume.
- Leaves are cut at trigger points (compact completion; day-boundary
  backstop), not identical to compact segments.
- A node's narrative budget is <= 1/10 of its direct input tokens — this
  module fixes the partition and the token accounting; generation output is
  checked mechanically after.
- Sealing is append-only: sealed nodes never change, and a pending carry of
  fewer than `min_count` units waits for the next trigger.
- A group of exactly one unit aliases (no summary node is generated).

This module is pure and deterministic: tree = f(unit stream, trigger
positions, params).
"""

from __future__ import annotations

from dataclasses import dataclass

COUNT_CAP = 15  # standard fan-in cap (kappa upper bound)
COUNT_CAP_SMALL = 20  # extended cap when all units are "small"
SMALL_UNIT_TOK = 1500  # "small unit" threshold (tokens); calibrated later
MIN_COUNT = 5  # kappa lower bound; fewer units carry to the next trigger
SRC_CAP = 150_000  # per-group source guard (tokens) = 10 x narrative hard cap
NARRATIVE_CAP_TOK = 15_000  # per-node narrative hard cap (tokens)


@dataclass(frozen=True)
class Unit:
    """A partitionable unit — a block at level 0, or a sealed node above it."""

    uid: str  # stable id
    tok: int  # source tokens (blocks) or node text tokens (nodes)
    span: tuple[int, int]  # inclusive message-index span
    at: tuple[str, str] = ("", "")  # (t_start, t_end)
    kind: str = "block"  # block | node | alias


@dataclass(frozen=True)
class NodeSpec:
    """A sealed node to be materialized (summarized) in the generation pass."""

    nid: str
    level: int  # 1-based: 1 = leaf
    units: tuple[Unit, ...]  # children, in stream order
    kind: str = "group"  # group | alias
    src_tok: int = 0
    span: tuple[int, int] = (0, 0)
    at: tuple[str, str] = ("", "")

    def budget_tok(self) -> int:
        """The node's narrative budget: min(source/10, hard cap), in tokens."""
        return min(self.src_tok // 10, NARRATIVE_CAP_TOK)


@dataclass(frozen=True)
class SealParams:
    """Tunable split parameters (defaults = the frozen v0.3 contract)."""

    cap: int = COUNT_CAP
    cap_small: int = COUNT_CAP_SMALL
    small_tok: int = SMALL_UNIT_TOK
    min_count: int = MIN_COUNT
    src_cap: int = SRC_CAP


@dataclass(frozen=True)
class SealResult:
    """The cascade output: materialization specs plus the unsealed carry."""

    nodes: tuple[NodeSpec, ...]
    pending: dict[int, tuple[Unit, ...]]
    max_level: int


def _even_sizes(n: int, k: int) -> list[int]:
    """Split `n` units into `k` chunks as evenly as possible (bigger first)."""
    base, extra = divmod(n, k)
    return [base + 1] * extra + [base] * (k - extra)


def _split_by_src(units: list[Unit], src_cap: int, min_count: int) -> list[list[Unit]]:
    """Re-split a group so each chunk's source stays within `src_cap`.

    The cap is loose: a single oversized unit forms its own chunk (aliased
    upstream), and a trailing chunk below `min_count` folds into the previous
    chunk rather than waiting a whole trigger cycle for one stray unit.
    """
    chunks: list[list[Unit]] = []
    cur: list[Unit] = []
    tot = 0
    for u in units:
        if cur and tot + u.tok > src_cap:
            chunks.append(cur)
            cur, tot = [u], u.tok
        else:
            cur.append(u)
            tot += u.tok
    if cur:
        chunks.append(cur)
    if len(chunks) >= 2 and len(chunks[-1]) < min_count:
        chunks[-2].extend(chunks.pop())
    return chunks


def split_units(
    units: list[Unit], params: SealParams | None = None
) -> tuple[list[list[Unit]], list[Unit]]:
    """Split an ordered unit list into sealable groups plus the carry.

    Returns `(groups, carry)`: each group has at least 2 units except when the
    source guard isolates one oversized unit; `carry` is the trailing units
    (fewer than `min_count`) deferred to the next trigger.
    """
    p = params or SealParams()
    n = len(units)
    if n == 0:
        return [], []
    eff_cap = p.cap_small if all(u.tok <= p.small_tok for u in units) else p.cap
    if n < p.min_count:
        return [], list(units)
    groups: list[list[Unit]]
    if n <= eff_cap:
        groups = [list(units)]
    else:
        k = (n + eff_cap - 1) // eff_cap
        groups = []
        pos = 0
        for s in _even_sizes(n, k):
            groups.append(list(units[pos : pos + s]))
            pos += s
    out: list[list[Unit]] = []
    for g in groups:
        out.extend(_split_by_src(g, p.src_cap, p.min_count))
    return out, []


def seal_cascade(
    triggers: list[tuple[str, list[Unit]]], params: SealParams | None = None
) -> SealResult:
    """Run the seal cascade over trigger batches, level by level.

    `triggers` is the ordered list of `(trigger_name, units)` batches (the
    blocks accumulated since the previous trigger). Each trigger flushes every
    level as far as it can; whatever cannot form a group stays pending.
    """
    p = params or SealParams()
    pending: dict[int, list[Unit]] = {0: []}
    nodes: list[NodeSpec] = []
    counters: dict[int, int] = {}

    def _new_uid(level: int) -> str:
        counters[level] = counters.get(level, 0) + 1
        return f"L{level}#{counters[level]}"

    for _tname, batch in triggers:
        pending.setdefault(0, [])
        pending[0].extend(batch)
        level = 0
        while True:
            units = pending.get(level, [])
            if not units:
                break
            groups, carry = split_units(units, p)
            if not groups:
                break  # below the kappa floor — the carry waits
            pending[level] = carry
            new_units: list[Unit] = []
            for g in groups:
                if len(g) == 1:
                    u = g[0]
                    nid = _new_uid(level + 1)
                    nodes.append(
                        NodeSpec(
                            nid=nid,
                            level=level + 1,
                            units=(u,),
                            kind="alias",
                            src_tok=u.tok,
                            span=u.span,
                            at=u.at,
                        )
                    )
                    new_units.append(Unit(uid=nid, tok=u.tok, span=u.span, at=u.at, kind="alias"))
                else:
                    src = sum(u.tok for u in g)
                    span = (g[0].span[0], g[-1].span[1])
                    at = (g[0].at[0], g[-1].at[1])
                    nid = _new_uid(level + 1)
                    nodes.append(
                        NodeSpec(
                            nid=nid,
                            level=level + 1,
                            units=tuple(g),
                            kind="group",
                            src_tok=src,
                            span=span,
                            at=at,
                        )
                    )
                    # Nominal node tokens = its budget (deterministic; actuals
                    # are measured after generation).
                    new_units.append(
                        Unit(
                            uid=nid,
                            tok=min(src // 10, NARRATIVE_CAP_TOK),
                            span=span,
                            at=at,
                            kind="node",
                        )
                    )
            pending.setdefault(level + 1, [])
            pending[level + 1].extend(new_units)
            if not new_units:
                break
            level += 1
    max_level = max([0] + [nd.level for nd in nodes])
    carried = {lvl: tuple(us) for lvl, us in pending.items() if us}
    return SealResult(nodes=tuple(nodes), pending=carried, max_level=max_level)
