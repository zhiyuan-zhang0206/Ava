"""The census reports reach and unbounded-input numbers from the selector's own graph."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.audit import impact_census


def _write(root: Path, name: str, text: str = "") -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _repo(root: Path) -> Path:
    _write(root, "pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["base/**/tests"]\n')
    _write(root, "base/__init__.py")
    _write(root, "base/core.py")
    _write(
        root,
        "base/leaf.py",
        "import importlib\ndef load(name):\n    importlib.import_module(name)\n",
    )
    _write(root, "base/a/tests/test_a.py", "import base.core\nimport base.leaf\n")
    _write(root, "base/b/tests/test_b.py", "import base.core\n")
    _write(
        root,
        ".test_durations",
        json.dumps({"base/a/tests/test_a.py::test": 60, "base/b/tests/test_b.py::test": 120}),
    )
    return root


def test_census_counts_shared_sources_and_unbounded_consumers(tmp_path: Path) -> None:
    report = impact_census.census(_repo(tmp_path))
    assert (report.tests, report.full_minutes) == (2, 3.0)
    assert report.sources_reached_by_every_test == 2  # base/__init__.py and base/core.py
    assert report.by_package == {"base": (3, 2)}
    assert (report.unbounded_sites, report.unbounded_sites_reached_by_every_test) == (1, 0)
    assert (report.tests_reaching_unbounded, report.tests_reaching_unbounded_minutes) == (1, 1.0)
    assert [(site.path, site.tests) for site in report.heaviest_unbounded] == [("base/leaf.py", 1)]


def test_cli_prints_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert impact_census.main(["--repo-root", str(_repo(tmp_path)), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["unbounded_sites"] == 1


def test_census_retains_unrelated_resource_unknown_in_the_same_file(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    with (root / "base/leaf.py").open("a") as source:
        source.write("def read(file):\n    return file.read_text()\n")
    report = impact_census.census(root)
    assert (report.unbounded_sites, report.tests_reaching_unbounded) == (2, 1)
    assert report.tests_reaching_unbounded_minutes == 1.0
    assert {site.reason for site in report.heaviest_unbounded} == {
        "importlib.import_module target is not bounded literal text",
        "Resource read has no proven repository or external path anchor",
    }
