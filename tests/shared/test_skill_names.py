"""`shared.skill_names` — the one dash/underscore fold.

The behaviours the rest of the system leans on: dash is what a human sees,
underscore is what Python reaches, and an inbound name in either spelling
(or an ecosystem `plugin:skill` colon) folds onto the same key. `find` is the
bridge for the surfaces where a name has to become a real directory or
registry key.
"""

import pytest

from shared.skill_names import (
    SkillIdentity,
    SkillIdentityMismatch,
    display_name,
    find,
    match_key,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ava-goal", "ava_goal"),
        ("ava_goal", "ava_goal"),
        ("write-a-pr-description", "write_a_pr_description"),
        ("ava-code:pr", "ava_code.pr"),
        ("ava-code.pr", "ava_code.pr"),
        ("gmail", "gmail"),
    ],
)
def test_match_key_folds_every_spelling_of_one_name(name: str, expected: str) -> None:
    assert match_key(name) == expected


def test_match_key_is_idempotent() -> None:
    """It is applied at several boundaries in sequence; folding twice must not
    drift."""
    assert match_key(match_key("ava-code:pr")) == match_key("ava-code:pr")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ava_goal", "ava-goal"),
        ("ava-goal", "ava-goal"),
        ("web_ai.deep_research", "web-ai.deep-research"),
    ],
)
def test_display_name_renders_the_canonical_dash_form(name: str, expected: str) -> None:
    assert display_name(name) == expected


def test_display_name_leaves_namespace_dots_alone() -> None:
    """Ava renders namespace separation with `.` — a bare name string cannot say
    which segment is a plugin boundary, so nothing turns a `.` into a `:`."""
    assert display_name("ava_code.pr") == "ava-code.pr"


def test_find_prefers_an_exact_hit() -> None:
    assert find("foo_bar", ["foo-bar", "foo_bar"]) == "foo_bar"


def test_find_maps_an_inbound_spelling_onto_the_real_one() -> None:
    """The CLI/API case: a caller types the canonical dash form at a directory
    or registry row that is still underscore on disk."""
    assert find("ava-goal", ["ava_goal", "gmail"]) == "ava_goal"
    assert find("ava_goal", ["ava-goal", "gmail"]) == "ava-goal"


def test_find_returns_none_when_nothing_matches() -> None:
    assert find("nope", ["ava-goal", "gmail"]) is None


def test_find_returns_none_on_an_ambiguous_pool() -> None:
    """Two candidates folding together is a collision the loader refuses
    outright, so there is no correct pick to make here. (A mixed spelling that
    hits neither candidate exactly is what makes the ambiguity reachable.)"""
    assert find("foo-bar_baz", ["foo-bar-baz", "foo_bar_baz"]) is None


# ─── SkillIdentity (design R2-B): one skill = one key, all constructors fold ─


def test_identity_constructors_fold_every_spelling() -> None:
    """from_dir / from_frontmatter / from_cli all fold to the same key and
    render the canonical display — a comparison can never forget the fold."""
    for spelling in ("ava-code", "ava_code"):
        ident = SkillIdentity.from_dir(spelling)
        assert ident.key == "ava_code"
        assert ident.display == "ava-code"
    assert SkillIdentity.from_cli("ava-code:pr").key == "ava_code.pr"
    assert SkillIdentity.from_cli("ava-code:pr").display == "ava-code:pr"
    assert SkillIdentity.from_frontmatter("web_ai.deep_research").key == "web_ai.deep_research"


def test_identity_verify_accepts_fold_equal_pair() -> None:
    """The designed pair: install-point directory (source) and frontmatter
    name (display claim) denoting one skill."""
    SkillIdentity.from_dir("ava_code").verify(SkillIdentity.from_frontmatter("ava-code"))


def test_identity_verify_raises_on_mismatch() -> None:
    """A directory `wechat-ocr` and a frontmatter name `wechat` are two
    different skills — the state that used to load under a name that was not
    its own (#980 / #1702 family)."""
    with pytest.raises(SkillIdentityMismatch):
        SkillIdentity.from_dir("wechat-ocr").verify(SkillIdentity.from_frontmatter("wechat"))


def test_identity_equality_and_hash_are_by_key() -> None:
    """Identities work directly as dict keys / set members where the fold
    matters — the B1 invariant in one line."""
    assert SkillIdentity.from_dir("ava-code") == SkillIdentity.from_cli("ava_code")
    assert len({SkillIdentity.from_dir("ava-code"), SkillIdentity.from_cli("ava_code")}) == 1
    assert SkillIdentity.from_dir("ava-code") != SkillIdentity.from_dir("ava-fleet")
