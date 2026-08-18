"""Selection rules for dated release tags — shared by release_cut and the update
track's `releases` mode, so the two can never disagree about "the newest release".
"""

from __future__ import annotations

from datetime import date

from shared.release_tags import parse_release_tag, pick_latest_tag


def test_parse_full_tag() -> None:
    t = parse_release_tag("v0.11.4-202607232230")
    assert t is not None
    assert t.name == "v0.11.4-202607232230"
    assert t.version == (0, 11, 4)
    assert t.day == date(2026, 7, 23)
    assert t.hhmm == "2230"


def test_parse_tag_without_hhmm() -> None:
    t = parse_release_tag("v0.11.4-20260723")
    assert t is not None
    assert t.hhmm is None


def test_parse_rejects_non_release_tags() -> None:
    for raw in (
        "v0.11.4",
        "0.11.4-20260723",
        "v0.11.4-2026072",
        "v0.11.4-2026072399",
        "release-20260723",
        "v0.11.4-20260723-extra",
    ):
        assert parse_release_tag(raw) is None, raw


def test_pick_latest_higher_version_wins() -> None:
    t = pick_latest_tag(["v0.8.1-20260701", "v0.8.2-20260601", "v0.8.0-20260801"])
    assert t is not None and t.name == "v0.8.2-20260601"  # version beats date


def test_pick_latest_hhmm_wins_same_version() -> None:
    t = pick_latest_tag(["v0.8.0-202608011200", "v0.8.0-20260801"])
    assert t is not None and t.name == "v0.8.0-202608011200"


def test_pick_latest_later_hhmm_wins_same_version() -> None:
    t = pick_latest_tag(["v0.8.0-202608011200", "v0.8.0-202608011300"])
    assert t is not None and t.name == "v0.8.0-202608011300"


def test_pick_latest_ignores_non_release_tags() -> None:
    t = pick_latest_tag(["not-a-tag", "v1.2.3-20260101", "main"])
    assert t is not None and t.name == "v1.2.3-20260101"


def test_pick_latest_none_when_no_tags() -> None:
    assert pick_latest_tag(["v1.0.0", "release-notes"]) is None
    assert pick_latest_tag([]) is None
