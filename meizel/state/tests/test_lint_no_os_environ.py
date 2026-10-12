"""`meizel/state/no_os_environ.py` — a typo'd explicit target must fail the gate.

An explicit path argument that does not exist used to scan nothing and exit 0;
it must now report the missing target on stderr and exit 1. An unreadable
member of an explicit directory (e.g. a dangling symlink) is skipped, not a
crash.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from meizel import lint_common
from meizel.state import no_os_environ as gate


def _none() -> frozenset[str]:
    return frozenset()


def test_explicit_missing_target_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo'd explicit path must fail the gate, not pass as a silent empty scan."""
    good = tmp_path / "ok.py"
    good.write_text("value = 1\n", encoding="utf-8")
    missing = tmp_path / "typo.py"
    assert gate.main([str(missing)], managed_aliases=_none) == 1
    assert str(missing) in capsys.readouterr().err
    assert gate.main([str(good), str(missing)], managed_aliases=_none) == 1


def test_directory_with_dangling_symlink_member_is_skipped(tmp_path: Path) -> None:
    """A broken *.py symlink inside an explicit directory must be skipped like
    any unreadable entry — the scan must not crash on it."""
    (tmp_path / "ok.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "dangling.py").symlink_to(tmp_path / "missing.py")
    assert gate.main([str(tmp_path)], managed_aliases=_none) == 0


@pytest.mark.parametrize("host", ["cli", "ops", "ava_builtins/plugins/p"])
def test_no_os_environ_default_scan_reaches_tests_in_dirs_it_never_scanned(
    tmp_path: Path, host: str
) -> None:
    """Rule 2 (setenv on a Settings-managed alias) read `tests/cli` and `tests/ops`; the
    same tests inside `cli/`, `ops/` and `ava_builtins/` must stay read."""
    body = """
        def test_x(monkeypatch) -> None:
            monkeypatch.setenv("AVA_DB_URL", "x")
    """
    for name in (*lint_common.FRAMEWORK_DIRS, "tests"):
        (tmp_path / name).mkdir(exist_ok=True)
    _write(tmp_path, f"{host}/tests/test_x.py", body)
    assert gate.main([], managed_aliases=lambda: frozenset({"AVA_DB_URL"}), repo_root=tmp_path) == 1


def test_no_os_environ_default_scan_still_reads_only_tests_in_those_dirs(tmp_path: Path) -> None:
    """The extra roots are read for test files only: Rule 1 does not start covering them."""
    for name in (*lint_common.FRAMEWORK_DIRS, "tests"):
        (tmp_path / name).mkdir(exist_ok=True)
    _write(tmp_path, "cli/pkg/mod.py", 'import os\n\nHOME = os.environ["HOME"]\n')
    assert gate.main([], managed_aliases=lambda: frozenset({"AVA_DB_URL"}), repo_root=tmp_path) == 0


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")
