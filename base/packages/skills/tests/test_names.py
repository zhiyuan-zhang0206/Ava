import pytest

from base.packages.skills.names import (
    SkillIdentity,
    SkillIdentityMismatch,
    display_name,
    find,
    match_key,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("meiva-goal", "meiva_goal"),
        ("meiva_goal", "meiva_goal"),
        ("write-a-pr-description", "write_a_pr_description"),
        ("meiva-memory:consolidation", "meiva_memory.consolidation"),
        ("meiva-memory.consolidation", "meiva_memory.consolidation"),
        ("gmail", "gmail"),
    ],
)
def test_match_key_folds_every_spelling_of_one_name(name: str, expected: str) -> None:
    assert match_key(name) == expected


def test_match_key_is_idempotent() -> None:
    assert match_key(match_key("meiva-memory:consolidation")) == match_key(
        "meiva-memory:consolidation"
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("meiva_goal", "meiva-goal"),
        ("meiva-goal", "meiva-goal"),
        ("web_ai.deep_research", "web-ai.deep-research"),
    ],
)
def test_display_name_renders_the_canonical_dash_form(name: str, expected: str) -> None:
    assert display_name(name) == expected


def test_display_name_leaves_namespace_dots_alone() -> None:
    assert display_name("meiva_memory.consolidation") == "meiva-memory.consolidation"


def test_find_prefers_an_exact_hit() -> None:
    assert find("foo_bar", ["foo-bar", "foo_bar"]) == "foo_bar"


def test_find_maps_an_inbound_spelling_onto_the_real_one() -> None:
    assert find("meiva-goal", ["meiva_goal", "gmail"]) == "meiva_goal"
    assert find("meiva_goal", ["meiva-goal", "gmail"]) == "meiva-goal"


def test_find_returns_none_when_nothing_matches() -> None:
    assert find("nope", ["meiva-goal", "gmail"]) is None


def test_find_returns_none_on_an_ambiguous_pool() -> None:
    assert find("foo-bar_baz", ["foo-bar-baz", "foo_bar_baz"]) is None


def test_identity_constructors_fold_every_spelling() -> None:
    for spelling in ("meiva-code", "meiva_code"):
        ident = SkillIdentity.from_dir(spelling)
        assert ident.key == "meiva_code"
        assert ident.display == "meiva-code"
    assert SkillIdentity.from_cli("meiva-memory:consolidation").key == "meiva_memory.consolidation"
    assert (
        SkillIdentity.from_cli("meiva-memory:consolidation").display == "meiva-memory:consolidation"
    )
    assert SkillIdentity.from_frontmatter("web_ai.deep_research").key == "web_ai.deep_research"


def test_identity_verify_accepts_fold_equal_pair() -> None:
    SkillIdentity.from_dir("meiva_code").verify(SkillIdentity.from_frontmatter("meiva-code"))


def test_identity_verify_raises_on_mismatch() -> None:
    with pytest.raises(SkillIdentityMismatch):
        SkillIdentity.from_dir("page-ocr").verify(SkillIdentity.from_frontmatter("page"))


def test_identity_equality_and_hash_are_by_key() -> None:
    assert SkillIdentity.from_dir("meiva-code") == SkillIdentity.from_cli("meiva_code")
    assert len({SkillIdentity.from_dir("meiva-code"), SkillIdentity.from_cli("meiva_code")}) == 1
    assert SkillIdentity.from_dir("meiva-code") != SkillIdentity.from_dir("meiva-fleet")
