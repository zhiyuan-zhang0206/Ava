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
    good = tmp_path / "ok.py"
    good.write_text("value = 1\n", encoding="utf-8")
    missing = tmp_path / "typo.py"
    assert gate.main([str(missing)], managed_aliases=_none) == 1
    assert str(missing) in capsys.readouterr().err
    assert gate.main([str(good), str(missing)], managed_aliases=_none) == 1


def test_directory_with_dangling_symlink_member_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "dangling.py").symlink_to(tmp_path / "missing.py")
    assert gate.main([str(tmp_path)], managed_aliases=_none) == 0


@pytest.mark.parametrize("host", ["cli", "ops", "ava_builtins/plugins/p"])
def test_no_os_environ_default_scan_reaches_tests_in_dirs_it_never_scanned(
    tmp_path: Path, host: str
) -> None:
    body = """
        def test_x(monkeypatch) -> None:
            monkeypatch.setenv("AVA_DB_URL", "x")
    """
    for name in (*lint_common.FRAMEWORK_DIRS, "tests"):
        (tmp_path / name).mkdir(exist_ok=True)
    _write(tmp_path, f"{host}/tests/test_x.py", body)
    assert gate.main([], managed_aliases=lambda: frozenset({"AVA_DB_URL"}), repo_root=tmp_path) == 1


def test_no_os_environ_default_scan_still_reads_only_tests_in_those_dirs(tmp_path: Path) -> None:
    for name in (*lint_common.FRAMEWORK_DIRS, "tests"):
        (tmp_path / name).mkdir(exist_ok=True)
    _write(tmp_path, "cli/pkg/mod.py", 'import os\n\nHOME = os.environ["HOME"]\n')
    assert gate.main([], managed_aliases=lambda: frozenset({"AVA_DB_URL"}), repo_root=tmp_path) == 0


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")
