"""End-to-end coverage of the ambient-state rule (Rule 9) through lcs.main(): a new site fails,
the frozen baseline must match reality in both directions, the base-revision guard is
shrink-only, a new lint cannot introduce baseline exemptions, and a stale list
entry fails. Rule semantics live in scripts/structure/ambient_state/tests/test_ambient_state.py."""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from scripts.lint import code_structure as lcs
from scripts.structure import ambient_state, baseline_shards

LINT_STUB = ambient_state.LINT


def _write(root: pathlib.Path, name: str, content: str) -> pathlib.Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _baseline(root: pathlib.Path, ambient: dict[str, int] | None = None) -> pathlib.Path:
    """Write the baseline as shards, replacing any already there; only this section is filled."""
    directory = root / baseline_shards.SHARD_DIR
    if directory.is_dir():
        for path in directory.rglob("*.json"):
            path.unlink()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "README.md").write_text("Structure baseline shards.\n", encoding="utf-8")
    data = {
        "directories": {},
        "files": {},
        "complexity": {},
        "nesting": {},
        "private_imports": {},
        "owner_bypasses": {},
        "path_imports": {},
        ambient_state.SECTION: ambient or {},
    }
    for name, shard in baseline_shards.split(data).items():
        pathlib.Path(f"{directory}/{name}.json").parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(f"{directory}/{name}.json").write_text(
            baseline_shards.render(shard), encoding="utf-8"
        )
    return directory


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
    monkeypatch.setenv("LINT_STRUCTURE_BASELINE_BASE", "HEAD")
    _baseline(tmp_path)
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "add", baseline_shards.SHARD_DIR)
    _git(tmp_path, "commit", "--quiet", "-m", "Empty baseline")
    return tmp_path


def _commit_base(repo: pathlib.Path, *, with_lint: bool) -> None:
    """Commit the working tree as the base revision; `with_lint` says whether the ambient
    lint already exists there (a stub file at the path the gate checks)."""
    if with_lint:
        _write(repo, LINT_STUB, "# the lint exists at this revision\n")
    _git(repo, "init", "--quiet")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "base")


# --- a new site fails; a frozen site passes ----------------------------------------------------


def test_a_new_ambient_site_fails_with_its_rule_and_the_way_out(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, "base/state.py", "_REGISTRY = {}\n")

    assert lcs.main([], repo_root=_repo) == 1
    output = capsys.readouterr().out
    assert "base/state.py:1:" in output
    assert "`ambient-container` `_REGISTRY`" in output
    assert "inject what is read to decide" in output


def test_free_floating_background_work_names_the_service_loop_alternative(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        _repo,
        "gateway/work.py",
        "import asyncio\n\nasync def kick(job):\n    asyncio.create_task(job())\n",
    )

    assert lcs.main([], repo_root=_repo) == 1
    output = capsys.readouterr().out
    assert "gateway/work.py:4:" in output
    assert "`asyncio-task` `kick`" in output
    assert "its own service loop" in output
    assert "per-iteration `async with asyncio.TaskGroup()`" in output


def test_a_frozen_site_passes(_repo: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(_repo, "base/state.py", "_REGISTRY = {}\n")
    _baseline(_repo, {"base/state.py::ambient-container:_REGISTRY": 1})
    _commit_base(_repo, with_lint=True)

    assert lcs.main([], repo_root=_repo) == 0
    assert capsys.readouterr().out == ""


def test_schedules_are_governed_by_this_rule_only(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`schedules/` joins Rule 9's scope; the other AST rules still stop at the packages."""
    _write(
        _repo,
        "schedules/daily.py",
        "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    import x\n\n_STATE = {}\n",
    )
    assert lcs.main([], repo_root=_repo) == 1
    assert "schedules/daily.py:6:" in capsys.readouterr().out

    _baseline(_repo, {"schedules/daily.py::ambient-container:_STATE": 1})
    _commit_base(_repo, with_lint=True)
    assert lcs.main([], repo_root=_repo) == 0
    assert capsys.readouterr().out == ""


def test_tests_skill_scripts_and_main_blocks_are_not_scanned(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, "base/tests/test_state.py", "_REGISTRY = {}\n")
    _write(_repo, "ava_builtins/skills/web/scripts/run.py", "_REGISTRY = {}\nmain()\n")
    _write(_repo, "base/__main__.py", "_REGISTRY = {}\n")
    _write(_repo, "base/cli.py", 'if __name__ == "__main__":\n    run()\n')

    assert lcs.main([], repo_root=_repo) == 0
    assert capsys.readouterr().out == ""


# --- the frozen count must match reality in both directions ----------------------------------------


def test_a_site_beyond_the_frozen_count_fails(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, "base/boot.py", "import atexit\n\natexit.register(a)\natexit.register(b)\n")
    _baseline(_repo, {"base/boot.py::import-time-call:atexit.register": 1})

    assert lcs.main([], repo_root=_repo) == 1
    output = capsys.readouterr().out
    assert "base/boot.py:3:" in output
    assert "base/boot.py:4:" in output
    assert "grew above its frozen count 1" in output


def test_a_fixed_site_left_in_the_baseline_fails_as_stale_until_it_is_lowered(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, "base/boot.py", "import atexit\n\natexit.register(a)\n")
    _baseline(_repo, {"base/boot.py::import-time-call:atexit.register": 2})

    assert lcs.main([], repo_root=_repo) == 1
    output = capsys.readouterr().out
    assert "stale ambient_state entry base/boot.py::import-time-call:atexit.register" in output
    assert "frozen at 2 but the code has 1 — lower it to 1" in output

    _write(_repo, "base/boot.py", "VALUE = 1\n")
    assert lcs.main([], repo_root=_repo) == 1
    assert "the code has 0 — remove it" in capsys.readouterr().out


@pytest.mark.parametrize(
    "key",
    [
        "base/state.py::made-up-rule:_REGISTRY",
        "base/state.py::ambient-container:",
        "scripts/state.py::ambient-container:_REGISTRY",
    ],
)
def test_a_baseline_key_with_an_unknown_rule_or_outside_the_scope_is_invalid(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str], key: str
) -> None:
    _baseline(_repo, {key: 1})

    assert lcs.main([], repo_root=_repo) == 1
    assert "invalid ambient_state entry" in capsys.readouterr().err


# --- the base-revision guard ------------------------------------------------------------------------


def test_the_baseline_cannot_gain_a_key_against_the_base_revision(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit_base(_repo, with_lint=True)

    _write(_repo, "base/state.py", "_REGISTRY = {}\n")
    _baseline(_repo, {"base/state.py::ambient-container:_REGISTRY": 1})

    assert lcs.main([], repo_root=_repo) == 1
    output = capsys.readouterr().out
    assert "added ambient_state entry base/state.py::ambient-container:_REGISTRY" in output
    assert "baseline is shrink-only" in output


def test_the_baseline_cannot_raise_a_count_against_the_base_revision(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, "base/boot.py", "import atexit\n\natexit.register(a)\n")
    _baseline(_repo, {"base/boot.py::import-time-call:atexit.register": 1})
    _commit_base(_repo, with_lint=True)

    _write(_repo, "base/boot.py", "import atexit\n\natexit.register(a)\natexit.register(b)\n")
    _baseline(_repo, {"base/boot.py::import-time-call:atexit.register": 2})

    assert lcs.main([], repo_root=_repo) == 1
    output = capsys.readouterr().out
    assert "raised ambient_state entry base/boot.py::import-time-call:atexit.register" in output
    assert "from 1 to 2" in output


def test_lowering_a_count_against_the_base_revision_passes(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, "base/boot.py", "import atexit\n\natexit.register(a)\natexit.register(b)\n")
    _baseline(_repo, {"base/boot.py::import-time-call:atexit.register": 2})
    _commit_base(_repo, with_lint=True)

    _write(_repo, "base/boot.py", "import atexit\n\natexit.register(a)\n")
    _baseline(_repo, {"base/boot.py::import-time-call:atexit.register": 1})

    assert lcs.main([], repo_root=_repo) == 0
    assert capsys.readouterr().out == ""


def test_a_renamed_site_in_the_same_file_cannot_carry_its_frozen_entry(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unlike the locality sections there is no pairing: trading a frozen `_OLD` for a new
    `_NEW` launders state through the baseline, so the fix is at the site."""
    _write(_repo, "base/state.py", "_OLD = {}\n")
    _baseline(_repo, {"base/state.py::ambient-container:_OLD": 1})
    _commit_base(_repo, with_lint=True)

    _write(_repo, "base/state.py", "_NEW = {}\n")
    _baseline(_repo, {"base/state.py::ambient-container:_NEW": 1})

    assert lcs.main([], repo_root=_repo) == 1
    assert "added ambient_state entry base/state.py::ambient-container:_NEW" in (
        capsys.readouterr().out
    )


def test_introducing_a_lint_cannot_freeze_new_exemptions(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Introducing a lint cannot grant an exemption absent from the base revision."""
    _write(_repo, "base/state.py", "_REGISTRY = {}\n")
    _commit_base(_repo, with_lint=False)

    _baseline(_repo, {"base/state.py::ambient-container:_REGISTRY": 1})
    _write(_repo, LINT_STUB, "# the lint arrives in this change\n")

    assert lcs.main([], repo_root=_repo) == 1
    assert "added ambient_state entry base/state.py::ambient-container:_REGISTRY" in (
        capsys.readouterr().out
    )


def test_introducing_a_lint_with_no_exemptions_passes(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, "base/state.py", "VALUE = 1\n")
    _commit_base(_repo, with_lint=False)
    _write(_repo, LINT_STUB, "# the lint arrives in this change\n")

    assert lcs.main([], repo_root=_repo) == 0
    assert capsys.readouterr().out == ""


def test_a_moved_file_carries_its_frozen_key_once_the_key_is_migrated(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, "base/state.py", "_REGISTRY = {}\n")
    _baseline(_repo, {"base/state.py::ambient-container:_REGISTRY": 1})
    _commit_base(_repo, with_lint=True)

    _git(_repo, "mv", "base/state.py", "base/registry.py")
    assert lcs.main([], repo_root=_repo) == 1
    assert "was not migrated after its file moved to base/registry.py" in capsys.readouterr().out

    _baseline(_repo, {"base/registry.py::ambient-container:_REGISTRY": 1})
    assert lcs.main([], repo_root=_repo) == 0
    assert capsys.readouterr().out == ""


# --- the closed lists -------------------------------------------------------------------------------


def test_a_list_entry_whose_site_is_gone_fails_the_gate(
    _repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        ambient_state.allow, "ALLOWED", {"base/state.py::hidden-singleton:table": "static"}
    )
    _write(_repo, "base/state.py", "from functools import cache\n\n@cache\ndef table(): ...\n")
    assert lcs.main([], repo_root=_repo) == 0
    assert capsys.readouterr().out == ""

    _write(_repo, "base/state.py", "VALUE = 1\n")
    assert lcs.main([], repo_root=_repo) == 1
    assert "stale ambient_state list entry base/state.py::hidden-singleton:table" in (
        capsys.readouterr().out
    )


def test_a_list_entry_for_a_deleted_file_fails_the_gate(
    _repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        ambient_state.allow, "SINK_FACADES", {"base/log/sink.py": "the log sink's buffer"}
    )

    assert lcs.main([], repo_root=_repo) == 1
    assert "base/log/sink.py:1: stale ambient_state list entry — the file no longer exists" in (
        capsys.readouterr().out
    )
