"""Split-reexport audits accept the submodule fallback for lean package inits."""

from scripts import audit_split_reexports as gate


def test_missing_accepts_submodule_fallback() -> None:
    """`from pkg import sub` resolves for a lean package __init__ (no re-exports)."""
    assert gate._missing("shared.metrics", {"metrics_logql"}) == []
    assert gate._missing("shared.metrics", {"ci_runs_metrics"}) == []


def test_missing_reports_unknown_names() -> None:
    assert gate._missing("shared.metrics", {"no_such_name_xyz"}) == ["no_such_name_xyz"]
