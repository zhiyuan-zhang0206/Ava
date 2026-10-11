"""Executed Python inputs feed complete or explicitly incomplete placement facts."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.structure import placement
from scripts.structure.placement_evidence import IncompleteReferenceEvidenceError
from scripts.structure.tests.patch_repo import make_repo

_PATH = "scripts/tests/test_probe.py"


def test_non_execution_samples_do_not_become_strong_references(tmp_path: Path) -> None:
    (tmp_path / "base").mkdir()
    (tmp_path / "agent").mkdir()
    (tmp_path / "base" / "config.py").touch()
    (tmp_path / "agent" / "child.py").touch()
    source = (
        "import sys, subprocess\n"
        "sample = 'import agent.child\\n'\n"
        "subprocess.run([sys.executable, '-c', 'import base.config'])\n"
    )
    found = placement.collect_reference_evidence(
        ast.parse(source), placement.ModuleIndex(tmp_path), _PATH
    )
    assert [(r.kind, r.module) for r in found.refs] == [("embedded-import", "base.config")]
    assert found.unresolved == []


def test_embedded_from_import_preserves_clause_deduplication(tmp_path: Path) -> None:
    (tmp_path / "base").mkdir()
    (tmp_path / "base" / "config.py").touch()
    source = "import sys, subprocess\nsubprocess.run([sys.executable, '-c', 'from base.config import x, y'])"
    refs = placement.collect_references(ast.parse(source), placement.ModuleIndex(tmp_path), _PATH)
    assert [(r.kind, r.module) for r in refs] == [("embedded-import", "base.config")]


def test_unknown_execution_keeps_known_references_but_legacy_list_fails(tmp_path: Path) -> None:
    (tmp_path / "base").mkdir()
    (tmp_path / "base" / "config.py").touch()
    tree = ast.parse(
        "import sys, subprocess, base.config\nsubprocess.run([sys.executable, '-c', make_code()])"
    )
    index = placement.ModuleIndex(tmp_path)
    evidence = placement.collect_reference_evidence(tree, index, _PATH)
    assert [r.module for r in evidence.refs] == ["base.config"]
    assert len(evidence.unresolved) == 1
    with pytest.raises(
        IncompleteReferenceEvidenceError, match=r"scripts/tests/test_probe\.py:2:"
    ) as raised:
        placement.collect_references(tree, index, _PATH)
    assert raised.value.evidence == evidence


def test_legacy_patch_adapter_carries_unknown_without_claiming_complete_placement(
    tmp_path: Path,
) -> None:
    make_repo(tmp_path)
    tree = ast.parse(
        "import sys, subprocess, base.config\nsubprocess.run([sys.executable, '-c', make_code()])"
    )
    index = placement.ModuleIndex(tmp_path)
    result = placement.legacy_patch_placement(_PATH, tree, index, list(ast.walk(tree)))
    assert result.placement.home == "base/config"
    assert [ref.module for ref in result.evidence.refs] == ["base.config"]
    assert [(gap.path, gap.line) for gap in result.evidence.unresolved] == [(_PATH, 2)]
    with pytest.raises(IncompleteReferenceEvidenceError):
        placement.place(_PATH, tree, index)


def test_resolved_option_does_not_hide_a_remaining_dynamic_source(tmp_path: Path) -> None:
    (tmp_path / "base").mkdir()
    (tmp_path / "base" / "config.py").touch()
    tree = ast.parse(
        "import sys, subprocess\n"
        "subprocess.run([sys.executable, '-X', 'utf8', '-c', 'import base.config', data])\n"
        "subprocess.run([sys.executable, '-W', 'ignore', '-c', build_source()])\n"
    )
    index = placement.ModuleIndex(tmp_path)
    evidence = placement.collect_reference_evidence(tree, index, _PATH)
    assert [(ref.kind, ref.module) for ref in evidence.refs] == [("embedded-import", "base.config")]
    assert [(gap.path, gap.line) for gap in evidence.unresolved] == [(_PATH, 3)]
    with pytest.raises(IncompleteReferenceEvidenceError) as raised:
        placement.collect_references(tree, index, _PATH)
    assert raised.value.evidence == evidence
