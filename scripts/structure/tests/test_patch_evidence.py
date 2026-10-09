"""Patch authority diagnostics retain incomplete execution facts independently of home inference."""

from __future__ import annotations

from pathlib import Path

from scripts.structure import patch_report, patch_targets, placement
from scripts.structure.tests.patch_repo import make_repo

_UNKNOWN = (
    "import sys, subprocess\nfrom base.net import retry\n"
    "retry.backoff()\nsubprocess.run([sys.executable, '-c', make_code()])\n"
    "def test_x(monkeypatch):\n    monkeypatch.setattr('base.net.retry._sleep', None)\n"
)


def test_legacy_consumer_exposes_unknown_as_incomplete_inference(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    result = patch_targets.analyze(
        "tests/test_probe.py", _UNKNOWN, patch_targets.Classifier(placement.ModuleIndex(root))
    )
    assert result.authorization is patch_targets.Authorization.LEGACY_INFERENCE
    assert (result.home, [site.cat for site in result.sites]) == ("base/net", ["B"])
    assert result.evidence is not None
    assert [(gap.path, gap.line) for gap in result.evidence.unresolved] == [
        ("tests/test_probe.py", 4)
    ]
    report = patch_report.render({"tests/test_probe.py": result})
    assert "legacy-inference" in report
    assert "do not certify dependency LCA" in report
    assert "`tests/test_probe.py:4`" in report
    assert "Python -c source is not a literal or single binding" in report


def test_completeness_diagnostic_keeps_the_existing_private_policy(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    classifier = patch_targets.Classifier(placement.ModuleIndex(root))
    normal = patch_targets.analyze("tests/test_probe.py", _UNKNOWN, classifier)
    diagnostic = patch_targets.analyze(
        "tests/test_probe.py", _UNKNOWN, classifier, include_unpatched=True
    )
    assert normal == diagnostic
    assert (diagnostic.home, [site.cat for site in diagnostic.sites]) == ("base/net", ["B"])
    assert diagnostic.evidence is not None and diagnostic.evidence.unresolved


def test_support_owner_does_not_need_incomplete_subject_inference(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    result = patch_targets.analyze(
        "base/net/tests/support.py",
        _UNKNOWN,
        patch_targets.Classifier(placement.ModuleIndex(root)),
    )
    assert (result.home, [site.cat for site in result.sites]) == ("base/net", ["B"])
    assert result.evidence is not None and result.evidence.unresolved


def test_strict_diagnostics_collect_unknown_even_without_a_patch_point(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    text = "import sys, subprocess\nsubprocess.run([sys.executable, '-c', builder()])"
    result = patch_targets.analyze(
        "base/net/tests/test_probe.py",
        text,
        patch_targets.Classifier(placement.ModuleIndex(root)),
        include_unpatched=True,
    )
    assert result.sites == ()
    assert result.evidence is not None and result.evidence.unresolved
    report = patch_report.render({"base/net/tests/test_probe.py": result})
    assert "0 test files with patch points" in report
    assert "1 unresolved execution inputs" in report
