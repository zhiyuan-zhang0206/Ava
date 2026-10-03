"""The path-import gate in `scripts.lint.code_structure`: a new site fails, a frozen one passes, a fixed one fails until its entry is removed, and a new key is refused with or without a base shard."""

from __future__ import annotations

import pathlib
import subprocess

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


def _baseline(root: pathlib.Path, frozen: dict[str, int] | None) -> None:
    """Write the baseline as shards, plus the shard directory's README.md:
    read_worktree() requires the directory to exist, and the README is what
    keeps git tracking it (and a committed base comparable) even with zero
    shards."""
    directory = root / baseline_shards.SHARD_DIR
    if directory.is_dir():
        for path in directory.glob("*.json"):
            path.unlink()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "README.md").write_text("Structure baseline shards.\n", encoding="utf-8")
    sections: dict[str, dict[str, int]] = {
        kind: {}
        for kind in (
            "directories",
            "files",
            "complexity",
            "nesting",
            "private_imports",
            "owner_bypasses",
        )
    }
    if frozen is not None:
        sections["path_imports"] = frozen
    for name, shard in baseline_shards.split(sections).items():
        # Single concatenated string, not chained `/`: keeps an adversarial shard
        # name from being treated as an absolute-path override.
        (pathlib.Path(f"{directory}/{name}.json")).write_text(
            baseline_shards.render(shard), encoding="utf-8"
        )


def _commit(root: pathlib.Path, message: str) -> None:
    for args in (("init", "--quiet"), ("add", "-A"), ("commit", "--quiet", "-m", message)):
        subprocess.run(  # noqa: S603 — fixed test commands, never external input.
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
    monkeypatch.setattr(lcs, "_REPO_ROOT", tmp_path)
    monkeypatch.delenv("LINT_STRUCTURE_BASELINE_BASE", raising=False)
    _baseline(tmp_path, {})
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


def test_a_frozen_path_import_passes(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, _SKILL, _HACK)
    _baseline(_repo, {_KEY: 1})

    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""


def test_a_fixed_path_import_fails_until_its_entry_is_removed(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_repo, _SKILL, "value = 1\n")
    _baseline(_repo, {_KEY: 1})

    assert lcs.main([]) == 1
    assert f"stale path_imports entry {_KEY} is frozen at 1 but the code has 0" in (
        capsys.readouterr().out
    )


def test_a_new_key_is_refused_even_when_no_base_shard_ever_named_the_section(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No shard file carries a fixed set of sections: a section with zero entries
    anywhere in a shard's history is indistinguishable from one explicitly frozen
    empty, so a freshly introduced key gets no one-time grace period."""
    _baseline(_repo, None)
    _commit(_repo, "Base with no path_imports entries anywhere")
    _write(_repo, _SKILL, _HACK)
    _baseline(_repo, {_KEY: 1})

    assert lcs.main([]) == 1
    assert f"added path_imports entry {_KEY} — baseline is shrink-only" in (capsys.readouterr().out)


def test_a_new_key_is_refused_once_the_section_exists_at_the_base(
    _repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(_repo, "Base with an empty path_imports section")
    _write(_repo, _SKILL, _HACK)
    _baseline(_repo, {_KEY: 1})

    assert lcs.main([]) == 1
    assert f"added path_imports entry {_KEY} — baseline is shrink-only" in (capsys.readouterr().out)
