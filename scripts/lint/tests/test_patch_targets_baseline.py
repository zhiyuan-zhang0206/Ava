"""Patch-target baseline fields cannot exempt current foreign-private patches."""

import json
from pathlib import Path

import pytest

from scripts.lint import code_structure
from scripts.lint import patch_targets as lint
from scripts.structure.tests.patch_repo import make_repo, write

_KEY = "tests/components/base/test_x.py::base.net.retry._sleep"
_TEST = (
    "from base.net import retry\nfrom base.db import pool\n\n"
    "def test_x(monkeypatch):\n    retry.backoff()\n    pool.acquire()\n"
    "    monkeypatch.setattr('base.net.retry._sleep', None)\n"
)


@pytest.mark.parametrize("entries", [{}, {_KEY: 1}, {_KEY: 100}, []])
def test_current_patch_target_fields_are_rejected_even_when_empty(entries: object) -> None:
    with pytest.raises(ValueError, match="unknown section 'patch_targets'"):
        code_structure._parse_baseline({"tests": json.dumps({"patch_targets": entries})})


@pytest.mark.parametrize("entries", [{}, {_KEY: 1}])
def test_historical_patch_counts_are_discarded_without_permitting_current_sites(
    entries: dict[str, int], tmp_path: Path
) -> None:
    assert code_structure._parse_baseline(
        {"tests": json.dumps({"patch_targets": entries})}, historical=True
    ) == {"ambient_state": {}}
    root = make_repo(tmp_path, {"tests/components/base/test_x.py": _TEST})
    assert lint.main([], repo_root=root) == 1


def test_unknown_current_baseline_fields_remain_rejected() -> None:
    with pytest.raises(ValueError, match="unknown section 'new_allowance'"):
        code_structure._parse_baseline({"tests": '{"new_allowance": {}}'})


def test_moving_a_test_cannot_create_a_private_patch_allowance(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"tests/components/base/test_x.py": _TEST})
    assert lint.main([], repo_root=root) == 1
    old = root / "tests/components/base/test_x.py"
    write(root, "base/tests/test_x.py", old.read_text())
    old.unlink()
    assert lint.main([], repo_root=root) == 1


@pytest.mark.parametrize("entries", [{_KEY: 0}, {_KEY: True}, {"../escape.py::a._x": 1}])
def test_malformed_historical_patch_counts_are_not_silently_accepted(entries: object) -> None:
    with pytest.raises(ValueError, match="invalid patch_targets entry"):
        code_structure._parse_baseline(
            {"tests": json.dumps({"patch_targets": entries})}, historical=True
        )
