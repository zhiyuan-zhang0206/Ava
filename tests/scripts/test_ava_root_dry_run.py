"""scripts/ava_root_dry_run.py: the `--workdir` guard (S2 resolved-path fix).

The scratch carve-out must survive `..` / symlink spellings: the guard
resolves the path before comparing, and the caller uses the resolved form for
every later step (mkdir / writes / the `--cleanup` rmtree).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import ava_root_dry_run as drill

_AVA = Path.home() / ".ava"
_SCRATCH = _AVA / "workspaces"


def test_relative_workdir_refused() -> None:
    with pytest.raises(SystemExit):
        drill._guard_workdir(Path("rel/path"))


def test_dotdot_forms_cannot_escape_the_scratch_carveout() -> None:
    with pytest.raises(SystemExit):
        # reads as a scratch path, resolves to ~/.ava
        drill._guard_workdir(_SCRATCH / "..")
    with pytest.raises(SystemExit):
        # a deeper traversal spelling of the same protected target
        drill._guard_workdir(_SCRATCH / "6174" / ".." / "..")


def test_symlink_into_a_protected_home_refused(tmp_path: Path) -> None:
    link = tmp_path / "ava-home-link"
    link.symlink_to(_AVA, target_is_directory=True)
    with pytest.raises(SystemExit):
        drill._guard_workdir(link)


def test_scratch_subtree_allowed_and_normalized() -> None:
    allowed = drill._guard_workdir(_SCRATCH / "6174" / "drill")
    assert allowed == (_SCRATCH / "6174" / "drill").resolve()
    # an interior `..` is normalized to the resolved form the script uses
    normalized = drill._guard_workdir(_SCRATCH / "6174" / "x" / "..")
    assert normalized == (_SCRATCH / "6174").resolve()
