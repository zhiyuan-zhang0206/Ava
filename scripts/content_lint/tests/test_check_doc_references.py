"""scripts/content_lint/check_doc_references.py: code samples are not links.

The link check exists to catch rot, so it must stay loud on a real dangling
target while staying silent on a path that is being *taught* rather than
followed. Markdown offers three shapes for a sample and the checker has to
recognize all of them — the indented one is what reported
`ava_builtins/plugins/ava_memory/template/MEMORY.md:13` as broken, on a template
whose whole job is to show the pointer format.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.content_lint import check_doc_references
from scripts.content_lint.check_doc_references import REPO, check_doc

_NO_COMMANDS: dict[tuple[str, ...], set[str]] = {}


def _targets(tmp_path: Path, body: str) -> list[str]:
    doc = tmp_path / "doc.md"
    doc.write_text(body)
    return [message for _lineno, message in check_doc(doc, _NO_COMMANDS)]


def test_prose_link_to_a_missing_file_is_reported(tmp_path: Path) -> None:
    assert _targets(tmp_path, "See [the note](missing.md).\n") == ["`missing.md` — no such file"]


def test_link_to_an_existing_file_is_not_reported(tmp_path: Path) -> None:
    (tmp_path / "there.md").write_text("hi")
    assert _targets(tmp_path, "See [the note](there.md).\n") == []


@pytest.mark.parametrize(
    ("shape", "body"),
    [
        ("fenced", "Format:\n\n```\n- [Title](path.md) — what it holds\n```\n"),
        ("indented", "Format:\n\n    - [Title](path.md) — what it holds\n"),
        ("inline", "Write `[Title](path.md)` on its own line.\n"),
    ],
)
def test_a_sample_path_is_not_a_link(shape: str, body: str, tmp_path: Path) -> None:
    assert _targets(tmp_path, body) == [], f"{shape} sample was read as a link"


def test_an_indented_block_stays_open_across_blank_lines(tmp_path: Path) -> None:
    body = "Format:\n\n    - [One](a.md)\n\n    - [Two](b.md)\n"
    assert _targets(tmp_path, body) == []


def test_a_nested_list_item_is_still_checked(tmp_path: Path) -> None:
    """Four spaces under a bullet is list continuation, not a code sample —
    exempting on indent alone would blind the checker to half the docs."""
    body = "- outer\n\n    - nested [the note](missing.md)\n"
    assert _targets(tmp_path, body) == ["`missing.md` — no such file"]


def test_a_link_after_an_indented_block_is_checked_again(tmp_path: Path) -> None:
    body = "Format:\n\n    - [Title](path.md)\n\nThen see [the note](missing.md).\n"
    assert _targets(tmp_path, body) == ["`missing.md` — no such file"]


def test_the_memory_template_is_clean() -> None:
    """The doc that motivated the exemption, checked as it actually ships."""
    template = REPO / "ava_builtins" / "plugins" / "ava_memory" / "template" / "MEMORY.md"
    assert check_doc(template, _NO_COMMANDS) == []


# `future/`'s exemptions (issue #1045): a plan may propose a flag that does
# not exist yet (skip_flags), but a link it names must still resolve unless
# marked `(planned)` (allow_planned) — the class that shipped clean through the
# old blanket skip when a cited path moved out from under it.


def test_a_docs_future_style_moved_link_still_fails(tmp_path: Path) -> None:
    """`allow_planned` opts a MARKED link out — an unmarked one is still rot."""
    doc = tmp_path / "doc.md"
    doc.write_text("See [the plan](moved.md) for detail.\n")
    problems = check_doc(doc, _NO_COMMANDS, skip_flags=True, allow_planned=True)
    assert [m for _lineno, m in problems] == ["`moved.md` — no such file"]


def test_the_planned_marker_exempts_the_link_it_follows(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("See [the future module](ops/identity.py) (planned) for detail.\n")
    problems = check_doc(doc, _NO_COMMANDS, skip_flags=True, allow_planned=True)
    assert problems == []


def test_the_planned_marker_is_ignored_outside_docs_future(tmp_path: Path) -> None:
    """Without `allow_planned` (every doc but `future/`), the marker is just
    prose — a dangling link stays dangling."""
    doc = tmp_path / "doc.md"
    doc.write_text("See [the future module](ops/identity.py) (planned) for detail.\n")
    problems = check_doc(doc, _NO_COMMANDS)
    assert [m for _lineno, m in problems] == ["`ops/identity.py` — no such file"]


def test_the_planned_marker_must_be_immediately_adjacent(tmp_path: Path) -> None:
    """A `(planned)` elsewhere on the line does not reach back to exempt an
    earlier, unrelated dangling link — "adjacent" means right after this link,
    not "somewhere on this line"."""
    doc = tmp_path / "doc.md"
    doc.write_text("See [one](missing-one.md) and [two](missing-two.md) (planned).\n")
    problems = check_doc(doc, _NO_COMMANDS, skip_flags=True, allow_planned=True)
    assert [m for _lineno, m in problems] == ["`missing-one.md` — no such file"]


# Wikilinks: `[[target]]` and `[[target|label]]` name a node of the OKF graph,
# so a target that resolves to none is rot — whichever form it is written in.


@pytest.fixture
def nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        check_doc_references, "_okf_nodes", lambda: frozenset({"pkg/docs/real.ava.okf.md"})
    )


@pytest.mark.usefixtures("nodes")
@pytest.mark.parametrize(
    "link",
    ["[[gone.ava.okf.md]]", "[[gone.ava.okf.md|the label]]", "[[../../gone.ava.okf.md|group]]"],
)
def test_a_dangling_wikilink_is_reported_with_or_without_a_label(link: str, tmp_path: Path) -> None:
    target = link[2:-2].split("|", maxsplit=1)[0]
    assert _targets(tmp_path, f"See {link} for detail.\n") == [f"`[[{target}]]` — no such OKF node"]


@pytest.mark.usefixtures("nodes")
@pytest.mark.parametrize("link", ["[[real.ava.okf.md]]", "[[real.ava.okf.md|the label]]"])
def test_a_resolving_wikilink_is_clean(link: str, tmp_path: Path) -> None:
    assert _targets(tmp_path, f"See {link} for detail.\n") == []


@pytest.mark.usefixtures("nodes")
@pytest.mark.parametrize(
    "body",
    [
        "Write `[[gone.ava.okf.md|label]]` for an edge.\n",
        "```\n[[gone.ava.okf.md|label]]\n```\n",
        "Format:\n\n    [[gone.ava.okf.md|label]]\n",
    ],
)
def test_a_wikilink_sample_is_not_a_link(body: str, tmp_path: Path) -> None:
    assert _targets(tmp_path, body) == []


@pytest.mark.usefixtures("nodes")
def test_the_planned_marker_exempts_a_wikilink_in_a_plan(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("Next: [[later.ava.okf.md|the node]] (planned), then [[gone.ava.okf.md]].\n")
    problems = check_doc(doc, _NO_COMMANDS, skip_flags=True, allow_planned=True)
    assert [m for _lineno, m in problems] == ["`[[gone.ava.okf.md]]` — no such OKF node"]


def test_skip_flags_suppresses_an_invalid_flag(tmp_path: Path) -> None:
    commands: dict[tuple[str, ...], set[str]] = {("ava",): {"--verbose"}}
    doc = tmp_path / "doc.md"
    doc.write_text("Run `ava --not-a-real-flag`.\n")
    assert check_doc(doc, commands) == [(1, "`ava --not-a-real-flag` — no such flag")]
    assert check_doc(doc, commands, skip_flags=True) == []


def test_live_cli_options_reject_wrong_command_and_accept_alias(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text(
        "Use `ava stop --keep-infra` or `ava agents context 7 --limit 8`.\n"
        "Wrong: `ava start --keep-infra`.\n"
    )
    assert check_doc(doc, check_doc_references.ava_flags()) == [
        (2, "`ava start --keep-infra` — no such flag")
    ]


# Skill `references/` backtick refs (Task #939) — a SKILL.md's backticked
# `` `references/<file>.md` `` pointers must resolve to a references/ dir of the
# skill or its ancestors. The docstring's axis-2 rationale deliberately leaves
# generic backtick paths unchecked, but this shape is unambiguous and rotted
# twice in three review rounds.


def _skill(tmp_path: Path, body: str, *, name: str = "SKILL.md") -> tuple[Path, list[str]]:
    skill_dir = tmp_path / ".agents" / "skills" / "demo"
    skill_dir.mkdir(parents=True, exist_ok=True)
    doc = skill_dir / name
    doc.write_text(body)
    return doc, [m for _l, m in check_doc(doc, _NO_COMMANDS)]


def test_skill_backtick_ref_to_missing_file_is_reported(tmp_path: Path) -> None:
    _doc, problems = _skill(tmp_path, "See `references/gone.md` for the model.\n")
    assert problems == ["`references/gone.md` — no such file (skill references/)"]


def test_skill_backtick_ref_to_existing_file_is_clean(tmp_path: Path) -> None:
    (tmp_path / ".agents" / "skills" / "demo" / "references").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".agents" / "skills" / "demo" / "references" / "there.md").write_text("hi")
    _doc, problems = _skill(tmp_path, "See `references/there.md`.\n")
    assert problems == []


def test_nested_skill_resolves_against_ancestor_library(tmp_path: Path) -> None:
    """A nested skill shares its parent's references/ library — the
    ava_builtins/skills/practice/ava-serious-engineering tree's ai-era/ and principles/
    skills all point at
    the root library."""
    root = tmp_path / "ava_builtins" / "skills" / "serious"
    (root / "references").mkdir(parents=True)
    (root / "references" / "book.md").write_text("hi")
    nested = root / "ai-era" / "child"
    nested.mkdir(parents=True)
    doc = nested / "SKILL.md"
    doc.write_text("See `references/book.md`.\n")
    problems = [m for _l, m in check_doc(doc, _NO_COMMANDS)]
    assert problems == []


def test_nested_skill_own_library_shadows_ancestor(tmp_path: Path) -> None:
    root = tmp_path / "ava_builtins" / "skills" / "serious"
    (root / "references").mkdir(parents=True)
    (root / "references" / "book.md").write_text("old")
    nested = root / "child"
    (nested / "references").mkdir(parents=True)
    (nested / "references" / "book.md").write_text("new")
    doc = nested / "SKILL.md"
    doc.write_text("See `references/book.md`.\n")
    problems = [m for _l, m in check_doc(doc, _NO_COMMANDS)]
    assert problems == []


def test_placeholder_skill_ref_is_skipped(tmp_path: Path) -> None:
    _doc, problems = _skill(tmp_path, "Write `references/<name>.md` for each.\n")
    assert problems == []


def test_non_skill_doc_backtick_ref_stays_unchecked(tmp_path: Path) -> None:
    """A plain doc's backticked `` `references/x.md` `` is deliberately not
    checked (axis-2 rationale: it may be a runtime path or a taught sample)."""
    _doc, problems = _skill(tmp_path, "See `references/gone.md`.\n", name="notes.md")
    assert problems == []


# ─── bare skills/<name>/ prefix refs (audit round 2, skills-plugins #2) ──────


def test_skill_bare_skills_prefix_ref_is_reported(tmp_path: Path) -> None:
    doc = tmp_path / "SKILL.md"
    doc.write_text(".venv/bin/python skills/gmail/reference/feed.py search\n", encoding="utf-8")
    problems = check_doc(doc, {})
    assert any("no top-level skills/" in msg for _, msg in problems)


def test_skill_load_dir_prefix_ref_is_clean(tmp_path: Path) -> None:
    doc = tmp_path / "SKILL.md"
    doc.write_text(
        "$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/gmail/reference/feed.py search\n",
        encoding="utf-8",
    )
    assert check_doc(doc, {}) == []


def test_skill_prose_about_load_dir_is_clean(tmp_path: Path) -> None:
    doc = tmp_path / "SKILL.md"
    doc.write_text(
        "Scripts run from `$AVA_HOME/skills/<name>/` (the load dir).\n", encoding="utf-8"
    )
    assert check_doc(doc, {}) == []
