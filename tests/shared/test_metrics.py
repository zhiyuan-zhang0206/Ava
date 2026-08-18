"""Unit tests for shared/metrics.py — the render helpers shared with the
SQL-aggregated metrics path.

These are the pure helpers (`pctiles`, `third_of`, `_fix_kinds`) that
`shared.metrics_aggregate` reuses so the math cannot drift between the SQL
aggregation and the rendered report. The per-row computation units they used
to accompany (EventRow/EventIndex/@metric_unit) were retired with the SQL
aggregation — see the module docstring in shared/metrics.py.
"""

from __future__ import annotations

from shared.metrics import _fix_kinds, pctiles, third_of


def test_fix_kinds_strips_count_suffix():
    assert _fix_kinds("ruff,ruff_format") == ["ruff", "ruff_format"]
    assert _fix_kinds("chinese_punct(3),missing_imports(2)") == [
        "chinese_punct",
        "missing_imports",
    ]
    assert _fix_kinds("") == []


def test_fix_kinds_drops_none_sentinel():
    # llm_repair / compile_failed paths log fixes="none" — not a real fix kind
    assert _fix_kinds("none") == []
    assert _fix_kinds("ruff,none") == ["ruff"]


def test_pctiles_pins_nearest_rank_values():
    # nearest-rank (index = floor(p*n)), not interpolated — lock the contract
    p = pctiles([float(i) for i in range(1, 11)])  # 1..10
    assert p["p50"] == 6.0
    assert p["p90"] == 10.0


def test_third_of_buckets():
    assert third_of(0, 9) == "early"
    assert third_of(4, 9) == "mid"
    assert third_of(8, 9) == "late"


def test_pctiles_empty_is_zero():
    assert pctiles([])["n"] == 0


def test_pctiles_basic():
    p = pctiles([1.0, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert p["n"] == 10
    assert p["max"] == 10
    assert p["mean"] == 5.5
