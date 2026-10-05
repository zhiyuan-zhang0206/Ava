"""Relocated contributor docs keep live checks and frozen-history boundaries."""

from pathlib import Path

import pytest
import yaml

from scripts.audit import module_moves
from scripts.content_lint import (
    check_doc_references,
    lint_doc_anchors,
    lint_doc_roster,
    lint_doc_symbols,
)

_REPO = Path(__file__).resolve().parents[3]


def test_procedural_scan_roots_follow_relocated_conventions() -> None:
    expected = _REPO / "docs" / "conventions"
    assert expected == lint_doc_anchors._DOCS_CONVENTIONS
    assert expected == lint_doc_symbols._DOCS_CONVENTIONS
    assert _REPO / "ops" / "docs" / "service-roster.md" == lint_doc_roster._SERVICE_ROSTER


@pytest.mark.parametrize("axis", ["decisions", "postmortems"])
def test_relocated_history_is_exempt_but_live_docs_still_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, axis: str
) -> None:
    history = tmp_path / "docs" / axis / "record.md"
    history.parent.mkdir(parents=True)
    history.write_text("See [the old implementation](missing.py).\n")
    live = tmp_path / "docs" / "conventions" / "guide.md"
    live.parent.mkdir(parents=True)
    live.write_text("See [the implementation](missing.py).\n")
    monkeypatch.setattr(check_doc_references, "REPO", tmp_path)
    monkeypatch.setattr(check_doc_references, "ava_flags", dict)
    monkeypatch.setattr(check_doc_references, "tracked_docs", lambda: [history])
    assert check_doc_references.main() == 0
    monkeypatch.setattr(check_doc_references, "tracked_docs", lambda: [live])
    assert check_doc_references.main() == 1
    assert module_moves.is_frozen(history.relative_to(tmp_path).as_posix())
    assert not module_moves.is_frozen(live.relative_to(tmp_path).as_posix())


def test_relocated_conventions_trigger_procedural_hooks() -> None:
    import re

    config = yaml.safe_load((_REPO / ".pre-commit-config.yaml").read_text())
    hooks = {hook["id"]: hook for repo in config["repos"] for hook in repo["hooks"]}
    assert re.search(hooks["lint-doc-roster"]["files"], "ops/docs/service-roster.md")
    assert re.search(hooks["lint-doc-roster"]["files"], "ops/roster/__init__.py")
    for hook_id in ("lint-doc-symbols", "lint-doc-anchors"):
        assert re.search(hooks[hook_id]["files"], "docs/conventions/runbook.md")
