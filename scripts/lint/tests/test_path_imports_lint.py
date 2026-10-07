"""Path-import sites fail directly; retired baseline fields cannot permit them."""

from __future__ import annotations

import json
import pathlib

import pytest

from scripts.lint import code_structure as lcs
from scripts.structure import baseline_shards
from tests.path_scoped.structure_tests import (
    _synthetic_ambient_allowlists as _synthetic_ambient_allowlists,
)
from tests.path_scoped.structure_tests import (
    _synthetic_decision_allowlists as _synthetic_decision_allowlists,
)

_SKILL = "ava_builtins/skills/demo/reference/run.py"


def _write(root: pathlib.Path, name: str, content: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _baseline(root: pathlib.Path) -> None:
    directory = root / baseline_shards.SHARD_DIR
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "README.md").write_text("Structure baseline shards.\n", encoding="utf-8")


@pytest.fixture
def _repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    monkeypatch.setattr(lcs, "_REPO_ROOT", tmp_path)
    monkeypatch.delenv("LINT_STRUCTURE_BASELINE_BASE", raising=False)
    _baseline(tmp_path)
    return tmp_path


_HACK = "import sys\nsys.path.insert(0, 'here')\n"


_KEY = f"{_SKILL}::sys.path"


def test_a_new_path_import_fails_the_gate(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, _SKILL, _HACK)

    assert lcs.main([]) == 1
    output = capsys.readouterr().out
    assert f"{_SKILL}:2: imports by file path (`sys.path`)" in output
    assert "thin entry point" in output


@pytest.mark.parametrize("frozen", [{}, {_KEY: 1}])
def test_retired_path_import_field_cannot_allow_a_site(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str], frozen: dict[str, int]
) -> None:
    _write(_repo, _SKILL, _HACK)
    _write(_repo, f"{baseline_shards.SHARD_DIR}/legacy.json", json.dumps({"path_imports": frozen}))
    assert lcs.main([]) == 1
    assert "unknown section 'path_imports'" in capsys.readouterr().err
