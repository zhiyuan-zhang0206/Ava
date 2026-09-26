"""End-to-end locality coverage through lcs.main(): the reach-in/stale-entry
lifecycle, the base-revision baseline guard (pairing, swaps, renames), and the
postgres-dial single-owner check. Unit-level rule semantics live in
test_locality_rules.py — this file is about the gate wiring around them."""

from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

from scripts import lint_code_structure as lcs


def _write(root: pathlib.Path, name: str, content: str) -> pathlib.Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _baseline(
    root: pathlib.Path,
    *,
    files: dict[str, int] | None = None,
    directories: dict[str, int] | None = None,
    complexity: dict[str, int] | None = None,
    nesting: dict[str, int] | None = None,
    private_imports: dict[str, int] | None = None,
    owner_bypasses: dict[str, int] | None = None,
) -> pathlib.Path:
    path = root / "scripts/structure/baseline.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "directories": directories or {},
                "files": files or {},
                "complexity": complexity or {},
                "nesting": nesting or {},
                "private_imports": private_imports or {},
                "owner_bypasses": owner_bypasses or {},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


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
    monkeypatch.delenv("LINT_STRUCTURE_BASELINE_BASE", raising=False)
    _baseline(tmp_path)
    return tmp_path


def _commit_baseline(repo: pathlib.Path, message: str) -> None:
    _git(repo, "init", "--quiet")
    _git(repo, "add", "scripts/structure/baseline.json")
    _git(repo, "commit", "--quiet", "-m", message)


# --- private_imports: the reach-in / stale-entry lifecycle -------------------


def test_a_new_reach_in_fails_the_gate(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, "shared/_priv/mod.py", "x = 1\n")
    _write(_repo, "gateway/importer.py", "from shared._priv import mod\n")

    assert lcs.main([]) == 1
    output = capsys.readouterr().out
    assert "gateway/importer.py:1:" in output
    assert "reaches private `shared._priv`" in output


def test_the_same_reach_in_frozen_in_the_baseline_passes(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, "shared/_priv/mod.py", "x = 1\n")
    _write(_repo, "gateway/importer.py", "from shared._priv import mod\n")
    _baseline(_repo, private_imports={"gateway/importer.py::shared._priv": 1})

    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""


def test_removing_the_reach_in_but_keeping_the_entry_fails_as_stale(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, "shared/_priv/mod.py", "x = 1\n")
    _write(_repo, "gateway/importer.py", "value = 1\n")
    _baseline(_repo, private_imports={"gateway/importer.py::shared._priv": 1})

    assert lcs.main([]) == 1
    output = capsys.readouterr().out
    assert "stale private_imports entry gateway/importer.py::shared._priv" in output
    assert "remove it" in output


def test_a_docs_only_twin_directory_does_not_change_a_clean_module_owner(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Adding a same-named OKF docs folder beside a module must not flip the
    owner resolution for an importer that was already clean."""
    _write(_repo, "shared/db.py", "def _restore(): ...\n")
    _write(_repo, "shared/user.py", "from shared.db import _restore\n")
    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""

    _write(_repo, "shared/db/db.ava.okf.md", "# docs\n")
    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""


# --- baseline guard: pairing, swaps, and renames on private_imports ---------


def test_baseline_guard_rejects_an_unpaired_new_private_imports_key(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit_baseline(_repo, "Freeze empty baseline")

    _write(_repo, "shared/_priv/mod.py", "x = 1\n")
    _write(_repo, "gateway/importer.py", "from shared._priv import mod\n")
    _baseline(_repo, private_imports={"gateway/importer.py::shared._priv": 1})

    assert lcs.main([]) == 1
    output = capsys.readouterr().out
    assert "added private_imports entry gateway/importer.py::shared._priv" in output
    assert (
        "added key without a same-file removal of the same private name: a split, "
        "move or swap cannot carry a frozen site" in output
    )


def test_baseline_guard_accepts_a_same_file_owner_move_keeping_its_leaf(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The owner package moves (`shared` -> `shared.sub`) but the private leaf
    name `_priv` is unchanged — that's the one legitimate same-file addition."""
    _baseline(_repo, private_imports={"gateway/importer.py::shared._priv": 1})
    _commit_baseline(_repo, "Freeze the old owner location")

    _write(_repo, "shared/sub/_priv/mod.py", "x = 1\n")
    _write(_repo, "gateway/importer.py", "from shared.sub._priv import mod\n")
    _baseline(_repo, private_imports={"gateway/importer.py::shared.sub._priv": 1})

    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""


def test_baseline_guard_rejects_a_same_file_leaf_swap(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Trading one file's frozen reach-in for an unrelated new one in the same
    file (different private name, possibly a different owner entirely) is not
    a legitimate move — it must be fixed at the site, not laundered through
    the baseline."""
    _baseline(_repo, private_imports={"gateway/importer.py::shared._old": 1})
    _commit_baseline(_repo, "Freeze the old reach-in")

    _write(_repo, "agent/_secret.py", "x = 1\n")
    _write(_repo, "gateway/importer.py", "from agent import _secret\n")
    _baseline(_repo, private_imports={"gateway/importer.py::agent._secret": 1})

    assert lcs.main([]) == 1
    output = capsys.readouterr().out
    assert "added private_imports entry gateway/importer.py::agent._secret" in output
    assert (
        "added key without a same-file removal of the same private name: a split, "
        "move or swap cannot carry a frozen site — route it through the door or owner" in output
    )


def test_rename_carry_over_migrated_key_passes(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, "shared/_priv/mod.py", "x = 1\n")
    _write(_repo, "gateway/importer.py", "from shared._priv import mod\n")
    _baseline(_repo, private_imports={"gateway/importer.py::shared._priv": 1})
    _git(_repo, "init", "--quiet")
    _git(_repo, "add", "-A")
    _git(_repo, "commit", "--quiet", "-m", "Freeze the reach-in")

    _git(_repo, "mv", "gateway/importer.py", "gateway/importer_moved.py")
    _baseline(_repo, private_imports={"gateway/importer_moved.py::shared._priv": 1})

    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""


def test_rename_carry_over_left_unmigrated_fails(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, "shared/_priv/mod.py", "x = 1\n")
    _write(_repo, "gateway/importer.py", "from shared._priv import mod\n")
    _baseline(_repo, private_imports={"gateway/importer.py::shared._priv": 1})
    _git(_repo, "init", "--quiet")
    _git(_repo, "add", "-A")
    _git(_repo, "commit", "--quiet", "-m", "Freeze the reach-in")

    _git(_repo, "mv", "gateway/importer.py", "gateway/importer_moved.py")
    # Baseline left untouched: still names the pre-move path.

    assert lcs.main([]) == 1
    output = capsys.readouterr().out
    assert (
        "private_imports entry gateway/importer.py::shared._priv was not migrated "
        "after its file moved to gateway/importer_moved.py" in output
    )


# --- owner_bypasses: the postgres-dial gate ----------------------------------


def test_a_new_dial_in_a_governed_module_fails_the_gate(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, "gateway/db.py", "import psycopg\npsycopg.connect('dsn')\n")

    assert lcs.main([]) == 1
    output = capsys.readouterr().out
    assert "gateway/db.py:2:" in output
    assert "bypasses the single owner of `postgres-dial`" in output


def test_a_dial_frozen_in_the_baseline_passes(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, "gateway/db.py", "import psycopg\npsycopg.connect('dsn')\n")
    _baseline(_repo, owner_bypasses={"gateway/db.py::postgres-dial": 1})

    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""
