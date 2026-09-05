"""Ava commit co-author trailer parsing."""

from shared.git_identity import parse_ava_coauthor_agent_ids


def test_parse_ava_coauthors_returns_every_valid_author_in_order() -> None:
    message = """Ship failure feedback

Co-authored-by: Ava #17
Co-authored-by: Human <human@example.com>
Co-authored-by: Ava #29
"""

    assert parse_ava_coauthor_agent_ids(message) == [17, 29]


def test_parse_ava_coauthors_returns_none_without_an_ava_trailer() -> None:
    assert (
        parse_ava_coauthor_agent_ids("Subject\n\nCo-authored-by: Human <human@example.com>") is None
    )


def test_parse_ava_coauthors_does_not_treat_body_text_as_a_trailer() -> None:
    message = """Subject

Co-authored-by: Ava #17
This is still explanatory body text.

Signed-off-by: Human <human@example.com>
"""

    assert parse_ava_coauthor_agent_ids(message) is None


def test_parse_ava_coauthors_ignores_malformed_ava_trailers() -> None:
    message = """Subject

Co-authored-by: Ava #bad
Co-authored-by: Ava #0
Co-authored-by:Ava #8
Co-authored-by: Ava #41
"""

    assert parse_ava_coauthor_agent_ids(message) == [41]
    assert parse_ava_coauthor_agent_ids("Subject\n\nCo-authored-by: Ava #bad") == []
