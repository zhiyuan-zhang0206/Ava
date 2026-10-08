"""Strict locality gate wiring: no baseline fields, including after renames.

Historical empty fields are readable but never permit measured sites.
"""

from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

from scripts.lint import code_structure as lcs
from scripts.structure import baseline_shards


def _write(root: pathlib.Path, name: str, content: str) -> pathlib.Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _baseline(root: pathlib.Path, sections: dict[str, dict[str, int]] | None = None) -> None:
    directory = root / baseline_shards.SHARD_DIR
    directory.mkdir(parents=True, exist_ok=True)
    for path in directory.glob("*.json"):
        path.unlink()
    (directory / "README.md").write_text("Structure baseline shards.\n", encoding="utf-8")
    if sections is not None:
        _write(root, f"{baseline_shards.SHARD_DIR}/legacy.json", json.dumps(sections))


def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 — arguments are fixed test commands, never external input.
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Structure gate test",
            "-c",
            "user.email=structure-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def _repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """A repo root wired to lcs._REPO_ROOT, with an all-empty baseline already in place."""
    monkeypatch.setattr(lcs, "_REPO_ROOT", tmp_path)
    monkeypatch.setenv("LINT_STRUCTURE_BASELINE_BASE", "HEAD")
    _baseline(tmp_path)
    _commit_baseline(tmp_path, "Empty baseline")
    return tmp_path


def _commit_baseline(repo: pathlib.Path, message: str) -> None:
    _git(repo, "init", "--quiet")
    _git(repo, "add", baseline_shards.SHARD_DIR)
    _git(repo, "commit", "--quiet", "--allow-empty", "-m", message)


def test_a_private_reach_in_fails_directly(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, "base/_priv/mod.py", "x = 1\n")
    _write(_repo, "gateway/importer.py", "from base._priv import mod\n")
    assert lcs.main([]) == 1
    assert "gateway/importer.py:1: reaches private `base._priv`" in capsys.readouterr().out


def test_a_new_dial_in_a_governed_module_fails_directly(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, "gateway/db.py", "import psycopg\ndef dial():\n    psycopg.connect('dsn')\n")
    assert lcs.main([]) == 1
    assert (
        "gateway/db.py:3: bypasses the single owner of `postgres-dial`" in capsys.readouterr().out
    )


def test_a_docs_twin_does_not_change_a_clean_module_owner(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, "base/db.py", "def _restore(): ...\n")
    _write(_repo, "base/user.py", "from base.db import _restore\n")
    assert lcs.main([]) == 0
    _write(_repo, "base/db/db.ava.okf.md", "# docs\n")
    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("kind", lcs.locality.STRICT_SECTIONS)
@pytest.mark.parametrize("entries", [{}, {"gateway/importer.py::base._priv": 1}])
def test_current_retired_fields_cannot_permit_sites(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str], kind: str, entries: dict[str, int]
) -> None:
    _baseline(_repo, {kind: entries})
    assert lcs.main([]) == 1
    assert f"unknown section '{kind}'" in capsys.readouterr().err


def test_historical_empty_retired_fields_are_read_without_permission(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _baseline(_repo, {kind: {} for kind in lcs.locality.STRICT_SECTIONS})
    _commit_baseline(_repo, "Historical empty locality sections")
    _baseline(_repo)
    _write(_repo, "base/_priv/mod.py", "x = 1\n")
    _write(_repo, "gateway/importer.py", "from base._priv import mod\n")
    assert lcs.main([]) == 1
    output = capsys.readouterr().out
    assert "reaches private `base._priv`" in output
    assert "invalid base baseline" not in output


@pytest.mark.parametrize("kind", lcs.locality.STRICT_SECTIONS)
def test_nonempty_retired_history_cannot_be_used_for_comparison(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    _baseline(_repo, {kind: {"gateway/importer.py::base._priv": 1}})
    _commit_baseline(_repo, "Invalid retired allowance")
    _baseline(_repo)
    assert lcs.main([]) == 1
    assert f"retired {kind} baseline must be empty" in capsys.readouterr().out


def test_renaming_a_private_reach_in_still_fails_directly(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, "base/_priv/mod.py", "x = 1\n")
    _write(_repo, "gateway/importer.py", "from base._priv import mod\n")
    _commit_baseline(_repo, "No locality allowance")
    _git(_repo, "add", "-A")
    _git(_repo, "commit", "--quiet", "-m", "Original reach-in")
    _git(_repo, "mv", "gateway/importer.py", "gateway/importer_moved.py")
    assert lcs.main([]) == 1
    output = capsys.readouterr().out
    assert "gateway/importer_moved.py:1: reaches private `base._priv`" in output
    assert "migrate the baseline" not in output
