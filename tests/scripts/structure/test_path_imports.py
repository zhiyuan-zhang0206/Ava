"""Rule 6: code under ava_builtins/ reaches other code through packages, never a file path.

Unit coverage of `path_imports.measure` (which forms count, which do not, and the
ava_builtins/ scope), then the gate lifecycle through lcs.main(): a new site
fails, a frozen one passes, a fixed one fails as stale, and the base-revision
guard refuses any new key once it can actually compare against a real base.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess

import pytest

from scripts.lint import lint_code_structure as lcs
from scripts.structure import baseline_shards, path_imports

_SKILL = "ava_builtins/skills/demo/reference/run.py"


def _sites(source: str, rel_path: str = _SKILL) -> dict[str, list[int]]:
    return path_imports.measure(ast.parse(source), rel_path)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("import sys\nsys.path.insert(0, 'x')\n", "sys.path"),
        ("import sys\nsys.path.append('x')\n", "sys.path"),
        ("import sys\nsys.path[:0] = ['x']\n", "sys.path"),
        ("import sys\nsys.path += ['x']\n", "sys.path"),
        ("import sys\nsys.path = ['x']\n", "sys.path"),
        ("import sys as s\ns.path.extend(['x'])\n", "sys.path"),
        ("from sys import path\npath.insert(0, 'x')\n", "sys.path"),
        ("import site\nsite.addsitedir('x')\n", "site.addsitedir"),
        ("from site import addsitedir as add\nadd('x')\n", "site.addsitedir"),
        (
            "import importlib.util\nimportlib.util.spec_from_file_location('m', 'x.py')\n",
            "importlib.util.spec_from_file_location",
        ),
        (
            "from importlib.util import spec_from_file_location\n"
            "spec_from_file_location('m', 'x.py')\n",
            "importlib.util.spec_from_file_location",
        ),
        (
            "import importlib.machinery\nimportlib.machinery.SourceFileLoader('m', 'x.py')\n",
            "importlib.machinery.SourceFileLoader",
        ),
        ("import runpy\nrunpy.run_path('x.py')\n", "runpy.run_path"),
    ],
)
def test_each_path_import_form_is_a_site(source: str, target: str) -> None:
    assert _sites(source) == {f"{_SKILL}::{target}": [2]}


@pytest.mark.parametrize(
    "source",
    [
        "import sys\nprint(sys.path)\n",
        "import sys\nfound = 'x' in sys.path\n",
        "items = []\nitems.insert(0, 'x')\n",
        "import os\npath = os.path\npath.join('a', 'b')\n",
        "code = \"runpy.run_path('x.py')\"\n",
    ],
)
def test_reading_sys_path_and_unrelated_calls_are_not_sites(source: str) -> None:
    assert _sites(source) == {}


def test_only_ava_builtins_is_in_scope() -> None:
    source = "import sys\nsys.path.insert(0, 'x')\n"
    assert _sites(source, "cli/python_install.py") == {}
    assert _sites(source, "ava/shell/coding_tools/claude.py") == {}


# --- the gate ------------------------------------------------------------------


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
