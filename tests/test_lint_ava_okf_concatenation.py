"""`scripts/lint_ava_okf.py` — concatenation defects (rules 12 + 13).

The W010 okf-split campaign replaced sections with "summary sentence +
[[wikilink]]", and repeatedly dropped the blank line that should have
followed — the sentence ran straight into the next header or bullet marker on
the same physical line, so it never renders (e.g. "...write-path]].## Next
Section" prints as one line, the header lost). Rule 13 catches the sibling
defect: the same header duplicated instead of the next section's own.

Both rules are commit-blocking (E-level, not W-level): a concatenation is a
rendering-breaking defect, not a style nit, and the fix is a one-line local
edit — exactly the graduation-test shape for a lint rather than a sweeper
class (conventions/lint-vs-sweeper.md).

Everything is asserted against constructed trees, never the real doc tree.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

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
        "description: A node used to exercise the concatenation rules.\n"
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


# ── rule 12: header/bullet glued to the preceding text ───────────────
def test_header_glued_after_wikilink_blocks(tmp_path, monkeypatch, capsys):
    """The exact shape found across the campaign: a sentence ending in a
    wikilink runs straight into the next header, no blank line."""
    _node(
        tmp_path,
        "a.ava.okf.md",
        "# A\n\nThe write path: [[b.ava.okf.md]].## Next Section\n\nMore prose.\n",
    )
    _node(tmp_path, "b.ava.okf.md")

    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 1, out
    assert "E012" in out
    assert "Header glued" in out


def test_bullet_glued_after_wikilink_blocks(tmp_path, monkeypatch, capsys):
    """The bullet-absorbed-into-the-previous-line shape."""
    _node(
        tmp_path,
        "a.ava.okf.md",
        "# A\n\n- The write path: [[b.ava.okf.md]].- Plugin hooks are also relevant\n",
    )
    _node(tmp_path, "b.ava.okf.md")

    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 1, out
    assert "E012" in out
    assert "Bullet glued" in out


def test_bullet_glued_after_plain_word_blocks(tmp_path, monkeypatch, capsys):
    """No wikilink involved — a bullet fused directly onto the end of the
    previous bullet's last word, the shared/shared.ava.okf.md shape found by
    the tree-wide sweep."""
    _node(
        tmp_path,
        "a.ava.okf.md",
        "# A\n\n- services must not import agent kernel- File line budget: 500\n",
    )

    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 1, out
    assert "E012" in out


def test_header_and_blank_line_is_clean(tmp_path, monkeypatch, capsys):
    """The fixed shape: a blank line separates the sentence from the header."""
    _node(
        tmp_path,
        "a.ava.okf.md",
        "# A\n\nThe write path: [[b.ava.okf.md]].\n\n## Next Section\n\nMore prose.\n",
    )
    _node(tmp_path, "b.ava.okf.md")

    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 0, out
    assert "E012" not in out


def test_language_name_before_hash_is_not_flagged(tmp_path, monkeypatch, capsys):
    """ "C#" is a letter directly followed by '#' + space + a capitalized word —
    the same surface shape as the bug, but a legitimate language mention. The
    header-glue lookbehind excludes any letter/digit immediately before the
    '#' run for exactly this reason."""
    _node(
        tmp_path,
        "a.ava.okf.md",
        "# A\n\nThe Windows helper is written in C# Managed code, built with csc.exe.\n",
    )

    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 0, out
    assert "E012" not in out


def test_backtick_quoted_header_name_in_prose_is_not_flagged(tmp_path, monkeypatch, capsys):
    """`# Capabilities` cited by name inside inline code, mid-sentence, is
    prose about a header — not a concatenation defect. Inline code spans are
    masked before the scan runs."""
    _node(
        tmp_path,
        "a.ava.okf.md",
        "# A\n\nInjected once into `# Capabilities` in the system prompt.\n",
    )

    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 0, out
    assert "E012" not in out


def test_hash_comment_inside_fenced_code_is_not_flagged(tmp_path, monkeypatch, capsys):
    """A Python comment glued to a period inside a fenced code block reads the
    same as the bug surface-level, but code fences are masked before scanning."""
    _node(
        tmp_path,
        "a.ava.okf.md",
        "# A\n\n```python\nx = load_config().# Comment glued after a call\n```\n",
    )

    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 0, out
    assert "E012" not in out


def test_headers_own_repeated_hash_run_is_not_flagged(tmp_path, monkeypatch, capsys):
    """A legitimate header's own '#' characters must never look like glue —
    the lookbehind excludes another '#' immediately before the run."""
    _node(tmp_path, "a.ava.okf.md", "# A\n\n### A Real Level-3 Header\n\nProse.\n")

    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 0, out
    assert "E012" not in out


# ── rule 13: duplicate consecutive headers ────────────────────────────
def test_duplicate_consecutive_header_blocks(tmp_path, monkeypatch, capsys):
    _node(
        tmp_path,
        "a.ava.okf.md",
        "# A\n\n## Key Dependencies\n\n## Key Dependencies\n\n- [[b.ava.okf.md]]\n",
    )
    _node(tmp_path, "b.ava.okf.md")

    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 1, out
    assert "E013" in out
    assert "Duplicate consecutive header" in out


def test_same_header_text_apart_is_not_flagged(tmp_path, monkeypatch, capsys):
    """The same header text reused for two unrelated subsections (each under
    its own parent heading, with real content between) is a legitimate
    pattern, not a duplicate — rule 13 only fires when nothing but blank lines
    separates the two."""
    _node(
        tmp_path,
        "a.ava.okf.md",
        "# A\n\n"
        "## First Tool\n\n### Semantics\n\nProse about the first tool.\n\n"
        "## Second Tool\n\n### Semantics\n\nProse about the second tool.\n",
    )

    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 0, out
    assert "E013" not in out


def test_single_header_is_not_flagged(tmp_path, monkeypatch, capsys):
    _node(tmp_path, "a.ava.okf.md", "# A\n\n## Just One Section\n\nProse.\n")

    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 0, out
    assert "E013" not in out
