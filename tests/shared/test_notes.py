"""shared.notes — the note model + walkers (audit #2448 Phase 1/2 pure-function
unit tests: no filesystem for parse/extract, tmp_path for walk)."""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.frontmatter import parse_frontmatter_typed
from shared.notes import (
    extract_md_links,
    normalize_tags,
    parse_note,
    walk_notes,
)

# ── parse_frontmatter_typed ──


class TestParseFrontmatterTyped:
    def test_parses_and_preserves_types(self) -> None:
        parsed = parse_frontmatter_typed("---\ntitle: X\ntags: [a, b]\ncount: 3\n---\n\nbody\n")
        assert parsed is not None
        fm, body = parsed
        assert fm == {"title": "X", "tags": ["a", "b"], "count": 3}  # tags stays a list
        assert body == "\nbody\n"

    def test_no_frontmatter_returns_none(self) -> None:
        assert parse_frontmatter_typed("# just a heading\n") is None

    def test_unterminated_returns_none(self) -> None:
        assert parse_frontmatter_typed("---\ntitle: X\n") is None

    def test_bad_yaml_returns_none(self) -> None:
        assert parse_frontmatter_typed("---\ntitle: [unclosed\n---\nb\n") is None

    def test_non_dict_returns_none(self) -> None:
        assert parse_frontmatter_typed("---\n- just\n- a list\n---\nb\n") is None

    def test_closing_fence_at_end_of_file(self) -> None:
        parsed = parse_frontmatter_typed("---\ntitle: X\n---")
        assert parsed == ({"title": "X"}, "")

    def test_crlf_and_bom_tolerated(self) -> None:
        parsed = parse_frontmatter_typed("\ufeff---\r\ntitle: X\r\n---\r\n\r\nbody\r\n")
        assert parsed is not None
        fm, body = parsed
        assert fm == {"title": "X"}
        assert body == "\nbody\n"


# ── normalize_tags ──


class TestNormalizeTags:
    def test_string_is_wrapped(self) -> None:
        assert normalize_tags("tech-ops") == ("tech-ops",)

    def test_list_keeps_only_strings(self) -> None:
        assert normalize_tags(["a", "b"]) == ("a", "b")
        assert normalize_tags(["a", 5, "b", None]) == ("a", "b")

    def test_other_types_are_empty(self) -> None:
        assert normalize_tags(5) == ()
        assert normalize_tags({"a": 1}) == ()
        assert normalize_tags(None) == ()
        assert normalize_tags([]) == ()


# ── parse_note ──


class TestParseNote:
    def test_maps_all_fields(self) -> None:
        note = parse_note(
            "---\ntitle: Alpha\ndescription: A note\ntags: [ava-internal]\n"
            "timestamp: 2026-01-02 03:04:05\nava_agent: '1609'\nava_machine: gateway-host\n"
            "---\n\n# Alpha\n",
            "health/alpha",
        )
        assert note is not None
        assert note.rel == "health/alpha"
        assert note.title == "Alpha"
        assert note.description == "A note"
        assert note.tags == ("ava-internal",)
        assert note.timestamp is not None  # datetime → str()
        assert note.ava_agent == "1609"
        assert note.ava_machine == "gateway-host"
        assert note.body == "\n# Alpha\n"

    def test_missing_optional_fields_are_none(self) -> None:
        note = parse_note("---\ntitle: Beta\n---\n\n# Beta\n", "beta")
        assert note is not None
        assert note.title == "Beta"
        assert note.description is None
        assert note.tags == ()
        assert note.timestamp is None
        assert note.ava_agent is None
        assert note.ava_machine is None

    def test_title_falls_back_to_rel(self) -> None:
        note = parse_note("---\ntags: [x]\n---\n\nb\n", "sub/note")
        assert note is not None
        assert note.title == "sub/note"

    def test_non_string_title_and_description_coerced(self) -> None:
        note = parse_note("---\ntitle: 42\ndescription: 7\n---\n\nb\n", "n")
        assert note is not None
        assert note.title == "42"
        assert note.description == "7"

    def test_non_note_returns_none(self) -> None:
        assert parse_note("# no frontmatter\n", "plain") is None
        assert parse_note("---\ntags: [unclosed\n---\nb\n", "bad") is None


# ── extract_md_links ──


class TestExtractMdLinks:
    def test_relative_link_resolves(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        edges = extract_md_links("See [Beta](sub/beta.md).\n", tmp_path, tmp_path, "alpha")
        assert edges == (("alpha", "sub/beta"),)

    def test_parent_link_resolves(self, tmp_path: Path) -> None:
        edges = extract_md_links(
            "See [Alpha](../alpha.md).\n", tmp_path / "sub", tmp_path, "sub/beta"
        )
        assert edges == (("sub/beta", "alpha"),)

    def test_url_anchor_empty_skipped(self, tmp_path: Path) -> None:
        body = "[Web](https://example.com) [Anchor](#sec) [Empty]() [Rel](x.md)\n"
        edges = extract_md_links(body, tmp_path, tmp_path, "alpha")
        assert edges == (("alpha", "x"),)

    def test_out_of_root_link_skipped_not_raised(self, tmp_path: Path) -> None:
        body = "[Out](../outside.md) [Abs](/etc/passwd)\n"
        assert extract_md_links(body, tmp_path, tmp_path, "alpha") == ()

    def test_no_links(self, tmp_path: Path) -> None:
        assert extract_md_links("# nothing\n", tmp_path, tmp_path, "alpha") == ()


# ── walk_notes ──


class TestWalkNotes:
    def test_walks_sorted_and_parses(self, tmp_path: Path) -> None:
        (tmp_path / "b.md").write_text("---\ntitle: B\n---\n\nb\n", encoding="utf-8")
        (tmp_path / "a.md").write_text("---\ntitle: A\n---\n\na\n", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "c.md").write_text("---\ntitle: C\n---\n\nc\n", encoding="utf-8")

        out = list(walk_notes(tmp_path))
        assert [(p.name, n.rel, n.title) for p, n in out] == [
            ("a.md", "a", "A"),
            ("b.md", "b", "B"),
            ("c.md", "sub/c", "C"),
        ]

    def test_skip_names_and_no_frontmatter(self, tmp_path: Path) -> None:
        (tmp_path / "MEMORY.md").write_text("---\ntitle: Idx\n---\n\n#\n", encoding="utf-8")
        (tmp_path / "plain.md").write_text("# no fm\n", encoding="utf-8")
        (tmp_path / "good.md").write_text("---\ntitle: G\n---\n\ng\n", encoding="utf-8")

        out = list(walk_notes(tmp_path, skip_names=frozenset({"MEMORY.md"})))
        assert [n.rel for _, n in out] == ["good"]

    def test_unreadable_file_warns_and_skips(self, tmp_path: Path) -> None:
        (tmp_path / "x.md").mkdir()  # a directory named *.md — read_text raises
        (tmp_path / "good.md").write_text("---\ntitle: G\n---\n\ng\n", encoding="utf-8")
        warnings: list[str] = []
        out = list(walk_notes(tmp_path, warnings=warnings))
        assert [n.rel for _, n in out] == ["good"]
        assert warnings == ["cannot read x.md"]

    def test_notes_are_frozen(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("---\ntitle: A\n---\n\na\n", encoding="utf-8")
        note = next(walk_notes(tmp_path))[1]
        with pytest.raises(AttributeError):
            note.title = "changed"  # type: ignore[misc]
