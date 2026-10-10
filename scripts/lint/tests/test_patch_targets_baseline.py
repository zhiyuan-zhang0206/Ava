"""Patch-target baseline fields cannot exempt current foreign-private patches."""

import json
from pathlib import Path

import pytest

from base.host.proc import run_bounded
from scripts.lint import code_structure
from scripts.lint import patch_targets as lint
from scripts.structure.tests.patch_repo import make_repo, write

_KEY = "tests/components/base/test_x.py::base.net.retry._sleep"
_TEST = (
    "from base.net import retry\nfrom base.db import pool\n\n"
    "def test_x(monkeypatch):\n    retry.backoff()\n    pool.acquire()\n"
    "    monkeypatch.setattr('base.net.retry._sleep', None)\n"
)


def _baseline(root: Path, sections: dict[str, object]) -> Path:
    directory = root / "scripts/structure/baseline"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "README.md").write_text("Structure baseline shards.\n", encoding="utf-8")
    shard = directory / "tests.json"
    shard.write_text(json.dumps(sections), encoding="utf-8")
    return shard


def _commit(root: Path) -> None:
    for args in (
        ("init", "--quiet"),
        ("add", "-A"),
        (
            "-c",
            "user.name=Structure test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "Historical baseline",
        ),
    ):
        run_bounded(
            ["git", "-C", str(root), *args], timeout=30, capture_output=True
        ).check_returncode()


@pytest.mark.parametrize("entries", [{}, {_KEY: 1}, {_KEY: 100}, []])
def test_current_patch_target_fields_are_rejected_even_when_empty(
    entries: object, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _baseline(tmp_path, {"patch_targets": entries})
    assert code_structure.main([], repo_root=tmp_path, baseline_base="HEAD") == 1
    assert "unknown section 'patch_targets'" in capsys.readouterr().err


@pytest.mark.parametrize("entries", [{}, {_KEY: 1}])
def test_historical_patch_counts_are_discarded_without_permitting_current_sites(
    entries: dict[str, int], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shard = _baseline(tmp_path, {"patch_targets": entries})
    _commit(tmp_path)
    shard.unlink()
    assert code_structure.main([], repo_root=tmp_path, baseline_base="HEAD") == 0
    assert capsys.readouterr().out == ""
    root = make_repo(tmp_path, {"tests/components/base/test_x.py": _TEST})
    assert lint.main([], repo_root=root) == 1


def test_unknown_current_baseline_fields_remain_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _baseline(tmp_path, {"new_allowance": {}})
    assert code_structure.main([], repo_root=tmp_path, baseline_base="HEAD") == 1
    assert "unknown section 'new_allowance'" in capsys.readouterr().err


def test_moving_a_test_cannot_create_a_private_patch_allowance(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"tests/components/base/test_x.py": _TEST})
    assert lint.main([], repo_root=root) == 1
    old = root / "tests/components/base/test_x.py"
    write(root, "base/tests/test_x.py", old.read_text())
    old.unlink()
    assert lint.main([], repo_root=root) == 1


@pytest.mark.parametrize("entries", [{_KEY: 0}, {_KEY: True}, {"../escape.py::a._x": 1}])
def test_malformed_historical_patch_counts_are_not_silently_accepted(
    entries: object, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shard = _baseline(tmp_path, {"patch_targets": entries})
    _commit(tmp_path)
    shard.unlink()
    assert code_structure.main([], repo_root=tmp_path, baseline_base="HEAD") == 1
    assert "invalid patch_targets entry" in capsys.readouterr().out
