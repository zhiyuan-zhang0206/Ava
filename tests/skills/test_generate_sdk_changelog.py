"""Tests for generate_sdk_changelog.py."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

_ref_dir = (
    Path(__file__).resolve().parents[2]
    / "ava_builtins"
    / "skills"
    / "ava-self-development"
    / "reference"
)
sys.path.insert(0, str(_ref_dir))
import generate_sdk_changelog as gsc  # noqa: E402  # type: ignore[import-not-found]

# The skill under test is a standalone reference file injected via
# sys.path at runtime — pyright cannot resolve its module type, so every
# call site reports Unknown. File-level downgrade of the two call-site
# rules keeps the rest of this file's strict checks intact (audit round-2
# tests-ci P1, task #1143).
# pyright: reportUnknownMemberType = warning
# pyright: reportUnknownArgumentType = warning


_INIT_V0 = (
    '"""ava package."""\n'
    '__all_for_ava__ = ["agents", "understand"]\n'
    "from . import agents as agents\n"
    "from ._x import understand as understand\n"
)

_AGENTS_V0 = (
    '"""Agent ops."""\n'
    '__all_for_ava__ = ["AgentRow", "gone", "changed", "renamed_from", "kept"]\n'
    "\n"
    "\n"
    "class AgentRow:\n"
    '    """A row."""\n'
    "\n"
    "\n"
    "def gone(x: int) -> None:\n"
    '    """Will be removed."""\n'
    "\n"
    "\n"
    "def changed(a: int) -> None:\n"
    '    """Gains a required param."""\n'
    "\n"
    "\n"
    "def renamed_from(p: float) -> int:\n"
    '    """Will be renamed."""\n'
    "    return 0\n"
    "\n"
    "\n"
    "def kept(a: int, b: int = 1) -> None:\n"
    '    """Stays put."""\n'
)

_X_V0 = (
    '"""Private."""\n'
    '__all_for_ava__ = ["understand"]\n'
    "\n"
    "\n"
    "def understand(prompt: str) -> str:\n"
    '    """Old understand."""\n'
    "    return prompt\n"
)

_AGENTS_V1 = (
    '"""Agent ops."""\n'
    '__all_for_ava__ = ["AgentRow", "changed", "renamed_to", "kept", "brand_new"]\n'
    "\n"
    "\n"
    "class AgentRow:\n"
    '    """A row."""\n'
    "\n"
    "\n"
    "def changed(a: int, b: int) -> None:\n"
    '    """Now needs b."""\n'
    "\n"
    "\n"
    "def renamed_to(p: float) -> int:\n"
    '    """Was renamed_from."""\n'
    "    return 0\n"
    "\n"
    "\n"
    "def kept(a: int, b: int = 1) -> None:\n"
    '    """Stays put."""\n'
    "\n"
    "\n"
    "def brand_new(z: str) -> bool:\n"
    '    """Fresh capability."""\n'
    "    return True\n"
)

_X_V1 = (
    '"""Private."""\n'
    '__all_for_ava__ = ["understand"]\n'
    "\n"
    "\n"
    "def understand(prompt: str, *, path: str) -> str:\n"
    '    """New understand."""\n'
    "    return prompt + path\n"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 — test git fixture, fixed argv
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _write(repo: Path, rel: str, content: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repo = tmp_path_factory.mktemp("gsc_repo")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")

    _write(repo, "ava/__init__.py", "".join(_INIT_V0))
    _write(repo, "ava/agents.py", "".join(_AGENTS_V0))
    _write(repo, "ava/_x.py", "".join(_X_V0))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "feat: seed sdk")
    _git(repo, "tag", "v0")

    _write(repo, "ava/agents.py", "".join(_AGENTS_V1))
    _write(repo, "ava/_x.py", "".join(_X_V1))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "feat(agents)!: drop gone, restructure helpers (#42)")
    _git(repo, "tag", "v1")
    return repo


def test_extract_surface_resolves_modules_and_reexports(repo: Path) -> None:
    cwd = Path.cwd()
    os.chdir(repo)
    try:
        surface = gsc.extract_api_surface("v0")
    finally:
        os.chdir(cwd)
    assert set(surface) == {"ava", "ava.agents"}
    understand = next(s for s in surface["ava"] if s.name == "understand")
    assert understand.kind == "function"
    assert understand.signature == "(prompt: str) -> str"
    assert "agents" not in {s.name for s in surface["ava"]}


def test_diff_added_removed_changed_renamed(repo: Path) -> None:
    cwd = Path.cwd()
    os.chdir(repo)
    try:
        old = gsc.extract_api_surface("v0")
        new = gsc.extract_api_surface("v1")
        diff = gsc.diff_api_surface(old, new)
    finally:
        os.chdir(cwd)
    assert {s.name for s in diff.added} == {"brand_new"}
    assert {s.name for s in diff.removed} == {"gone"}
    assert [(o.name, n.name) for o, n in diff.renamed] == [("renamed_from", "renamed_to")]
    by_name = {c[0].name: c for c in diff.changed}
    assert by_name["changed"][4] is True
    assert "now requires b" in by_name["changed"][3]


def test_parse_breaking_commits_marker_and_pr(repo: Path) -> None:
    cwd = Path.cwd()
    os.chdir(repo)
    try:
        commits = gsc.parse_breaking_commits("v0", "v1")
    finally:
        os.chdir(cwd)
    assert len(commits) == 1
    c = commits[0]
    assert c.pr_number == 42
    assert c.module == "ava.agents"


def test_generate_entry_sections(repo: Path) -> None:
    cwd = Path.cwd()
    os.chdir(repo)
    try:
        out = gsc.generate_entry("v0", "v1", version="v1.0.0", when=date(2026, 6, 26))
    finally:
        os.chdir(cwd)
    assert out.startswith("## [v1.0.0]")
    assert "### Breaking Changes" in out
    assert "### Added" in out
    assert "### Removed" in out
    assert "Fresh capability" in out


def test_generate_entry_empty_range(repo: Path) -> None:
    cwd = Path.cwd()
    os.chdir(repo)
    try:
        out = gsc.generate_entry("v0", "v0", when=date(2026, 6, 26))
    finally:
        os.chdir(cwd)
    assert "_No SDK-visible changes._" in out


def test_splice_entry() -> None:
    existing = "# SDK Changelog\n\nheader\n\n## [old]\n\nx\n"
    entry = "## [new]\n\ny\n"
    result = gsc._splice_entry(existing, entry)
    assert result.startswith("# SDK Changelog")
    assert "## [old]" in result


def test_splice_entry_idempotent_same_label() -> None:
    existing = "# SDK Changelog\n\nheader\n\n## [v1.0.0] — 2026-06-26\n\nold body\n"
    entry = "## [v1.0.0] — 2026-06-29\n\nnew body\n"
    result = gsc._splice_entry(existing, entry)
    assert result.count("## [v1.0.0]") == 1
    assert "new body" in result
    assert "old body" not in result


def test_splice_entry_keeps_other_labels() -> None:
    existing = "# SDK Changelog\n\nheader\n\n## [v1.0.0]\n\nold\n"
    entry = "## [v2.0.0]\n\nnew\n"
    result = gsc._splice_entry(existing, entry)
    assert result.index("## [v2.0.0]") < result.index("## [v1.0.0]")
    assert "old" in result
    assert "new" in result
