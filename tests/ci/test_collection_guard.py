"""Locks tests/fixtures/collection_guard.py: one collector node per test directory.

pytest 9 binds a conftest's fixtures to the first `Directory` node of its
directory and matches them by node identity, so positional paths that leave a
directory and come back to it build a second node whose tests lose the
conftest's fixtures (autouse ones included). The guard groups the paths before
collection and stops a run whose collection still splits a directory.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from tests.fixtures.collection_guard import pytest_collection_modifyitems, pytest_configure

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SUB_CONFTEST = """
import pytest

@pytest.fixture
def marker():
    return "sub"

@pytest.fixture(autouse=True)
def _sub_autouse():
    return None
"""
_SUB_TEST = """
def test_{name}(marker, request):
    assert marker == "sub"
    assert "_sub_autouse" in request.fixturenames
"""


def _interleaved_tree(root: Path) -> list[str]:
    """`pkg/sub/test_a.py`, `pkg/test_top.py`, `pkg/sub/test_b.py` — in that order."""
    sub = root / "pkg" / "sub"
    sub.mkdir(parents=True)
    (sub / "conftest.py").write_text(_SUB_CONFTEST, encoding="utf-8")
    for name in ("a", "b"):
        (sub / f"test_{name}.py").write_text(_SUB_TEST.format(name=name), encoding="utf-8")
    (root / "pkg" / "test_top.py").write_text("def test_top():\n    pass\n", encoding="utf-8")
    return [str(sub / "test_a.py"), str(root / "pkg" / "test_top.py"), str(sub / "test_b.py")]


def _run_pytest(paths: list[str], *plugin: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    # A stray addopts (say a local `-n 4`) would re-shape the inner run.
    env.pop("PYTEST_ADDOPTS", None)
    return subprocess.run(  # noqa: S603 — our own interpreter, synthetic tmp paths
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *plugin, *paths],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_pytest_still_hides_the_conftest_from_a_revisited_directory(tmp_path: Path) -> None:
    """The upstream behaviour the guard works around. When this starts passing
    without the guard, the workaround can go."""
    proc = _run_pytest(_interleaved_tree(tmp_path))
    assert proc.returncode != 0, proc.stdout[-2000:]
    assert "fixture 'marker' not found" in proc.stdout, proc.stdout[-2000:]


def test_interleaved_paths_keep_every_test_under_its_conftest(tmp_path: Path) -> None:
    proc = _run_pytest(_interleaved_tree(tmp_path), "-p", "tests.fixtures.collection_guard")
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert "3 passed" in proc.stdout, proc.stdout[-2000:]


def test_positional_paths_are_grouped_by_directory() -> None:
    config = SimpleNamespace(
        args=["tests/components/agent/a.py", "tests/b.py", "tests/components/agent/c.py::test_x"]
    )
    pytest_configure(cast(pytest.Config, config))
    assert config.args == [
        "tests/b.py",
        "tests/components/agent/a.py",
        "tests/components/agent/c.py::test_x",
    ]


def test_a_directory_split_across_two_nodes_stops_the_run(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    first = pytest.Dir.from_parent(request.session, path=tmp_path)
    second = pytest.Dir.from_parent(request.session, path=tmp_path)
    items = [
        SimpleNamespace(iter_parents=lambda node=node: iter([node])) for node in (first, second)
    ]
    with pytest.raises(pytest.UsageError, match="collected twice"):
        pytest_collection_modifyitems(cast(list[pytest.Item], items))
