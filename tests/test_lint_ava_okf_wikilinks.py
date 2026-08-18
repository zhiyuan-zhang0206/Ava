"""`scripts/lint_ava_okf.py` — wikilink resolution diagnostics (rules 8 + 11).

A `[[wikilink]]` is the node-graph edge syntax, so its universe is the
`.ava.okf.md` files and nothing else: a link to a `decisions/` record can
never resolve, no matter how plainly the file exists on disk. W008 has to say
that, because "target not found" sends the reader looking for a missing file
instead of at the axis mix-up.

The other half is the resolver's unique-basename fallback, which rescues a
target whose *path* is wrong and keeps rescuing it until a second node takes
that basename — at which point an untouched link starts reporting W008. W011
reports the mismatch while the link still works.

Everything is asserted against constructed trees (the linter is driven with a
tmp dir as its repo root), never against the real doc tree: "the repo has no
W008" would pass the moment the rule stopped firing at all.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

# The linter imports build_okf_data as a top-level module — running it by path
# puts scripts/ on sys.path[0], so importing it here has to do the same.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
_lint = importlib.import_module("scripts.lint_ava_okf")


def _node(tmp_path: Path, rel: str, body: str = "") -> Path:
    """Write a minimal valid OKF node at `rel` (no other rule may fire on it)."""
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: doc\n"
        f"title: {Path(rel).name}\n"
        "description: A node used as a wikilink source or target.\n"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return path


def _lint_tmp(tmp_path, monkeypatch, capsys) -> tuple[int, str]:
    """Lint `tmp_path` as the repo root; return (effective exit code, stdout)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["lint_ava_okf.py"])
    code = 0
    try:
        _lint.main()
    except SystemExit as exc:
        code = int(exc.code or 0)
    return code, capsys.readouterr().out


# ── rule 8: the target is a real file, but not a node ────────────────
@pytest.mark.parametrize(
    "target",
    [
        "2026-07-29-some-decision.md",  # bare name, found by basename across the repo
        "decisions/2026-07-29-some-decision.md",  # as written from the repo root
    ],
)
def test_non_node_target_names_the_file_and_the_remedy(tmp_path, monkeypatch, capsys, target):
    """A wikilink to a decision record: the diagnostic names the file it found, says
    it is not a node, and points at the markdown link that is the actual fix."""
    decision = tmp_path / "decisions/2026-07-29-some-decision.md"
    decision.parent.mkdir(parents=True)
    decision.write_text("# Why\n", encoding="utf-8")
    _node(tmp_path, "shared/machine.ava.okf.md", f"Liveness is the live probe ([[{target}]]).")

    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 0, out  # W008 is a warning, not a block
    assert "W008" in out
    assert "not an OKF node" in out
    assert "decisions/2026-07-29-some-decision.md" in out  # what it found
    assert "markdown link" in out  # what to do instead
    assert "1 warning(s)" in out


def test_markdown_link_to_a_decision_record_is_clean(tmp_path, monkeypatch, capsys):
    """The remedy the message names really is clean — the linter reads wikilinks
    only, so citing a non-node axis as a markdown link reports nothing."""
    decision = tmp_path / "decisions/2026-07-29-some-decision.md"
    decision.parent.mkdir(parents=True)
    decision.write_text("# Why\n", encoding="utf-8")
    _node(
        tmp_path,
        "shared/machine.ava.okf.md",
        "Liveness is the live probe ([why](../decisions/2026-07-29-some-decision.md)).",
    )

    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 0, out
    assert "0 warning(s)" in out


def test_target_that_exists_nowhere_reports_a_plain_miss(tmp_path, monkeypatch, capsys):
    """No file of that name anywhere: the axis explanation would be a wrong guess,
    so the message stays the plain "not found"."""
    _node(tmp_path, "shared/machine.ava.okf.md", "See [[no-such-node.ava.okf.md]].")

    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 0, out
    assert "W008: Wikilink target not found: [[no-such-node.ava.okf.md]]" in out
    assert "not an OKF node" not in out


def test_url_target_is_not_reported_as_a_doc(tmp_path, monkeypatch, capsys):
    """A URL is nothing on disk, so it gets the plain miss — never the axis message,
    which would send the reader hunting for a same-named file in an axis dir."""
    doc = tmp_path / "conventions/runbook.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# Runbook\n", encoding="utf-8")
    _node(tmp_path, "shared/machine.ava.okf.md", "See [[https://example.test/runbook.md]].")

    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 0, out
    assert "W008: Wikilink target not found:" in out
    assert "not an OKF node" not in out


def test_ambiguous_basename_does_not_resolve(tmp_path, monkeypatch, capsys):
    """The basename fallback requires a *unique* match — with the name taken twice,
    a bare-basename link resolves to neither and reports W008."""
    _node(tmp_path, "a/skills.ava.okf.md")
    _node(tmp_path, "b/skills.ava.okf.md")
    _node(tmp_path, "c/citing.ava.okf.md", "See [[skills.ava.okf.md]].")

    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 0, out
    assert "W008: Wikilink target not found: [[skills.ava.okf.md]]" in out


# ── rule 11: the target resolves, but its path does not exist ────────
def test_wrong_path_rescued_by_the_basename_fallback_warns(tmp_path, monkeypatch, capsys):
    """`[[wrong/dir/x]]` still reaches `real/dir/x` by basename. The link works, so
    this is a warning — and it names the path the node actually has."""
    _node(tmp_path, "real/dir/target.ava.okf.md")
    _node(tmp_path, "elsewhere/citing.ava.okf.md", "See [[wrong/dir/target.ava.okf.md]].")

    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 0, out  # non-blocking: the edge is still built
    assert "W011" in out
    assert "[[wrong/dir/target.ava.okf.md]]" in out
    assert "real/dir/target.ava.okf.md" in out
    assert "1 warning(s)" in out


def test_stale_relative_prefix_warns(tmp_path, monkeypatch, capsys):
    """The shape this rule was written for: a link copied between siblings keeps a
    directory prefix that is already part of its own directory, so the relative read
    misses and only the basename saves it."""
    _node(tmp_path, "services/healthchecks/verdict.ava.okf.md")
    _node(
        tmp_path,
        "services/healthchecks/probe.ava.okf.md",
        "See [[healthchecks/verdict.ava.okf.md]].",
    )

    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 0, out
    assert "W011" in out
    assert "services/healthchecks/verdict.ava.okf.md" in out


@pytest.mark.parametrize(
    "target",
    [
        "real/dir/target.ava.okf.md",  # correct, read from the repo root
        "dir/target.ava.okf.md",  # correct, read from the citing node's directory
        "target.ava.okf.md",  # bare basename: the intended cross-tree form
    ],
)
def test_paths_that_denote_their_target_are_clean(tmp_path, monkeypatch, capsys, target):
    """W011 fires on a path that plays no part in the resolution — never on one that
    does, and never on a bare basename, which carries no path to be wrong."""
    _node(tmp_path, "real/dir/target.ava.okf.md")
    _node(tmp_path, "real/citing.ava.okf.md", f"See [[{target}]].")

    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 0, out
    assert "0 warning(s)" in out


def test_labelled_wikilink_is_checked_by_its_target(tmp_path, monkeypatch, capsys):
    """`[[target|label]]`: the label is display text, so the rules read the target
    half — a wrong path is caught through a label just as without one."""
    _node(tmp_path, "real/dir/target.ava.okf.md")
    _node(tmp_path, "elsewhere/citing.ava.okf.md", "See [[wrong/dir/target.ava.okf.md|Target]].")

    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 0, out
    assert "W011" in out
    assert "1 warning(s)" in out
