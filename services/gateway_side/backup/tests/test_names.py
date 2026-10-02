"""The managed-dump naming grammar: what counts as a dump, and the instant a name carries."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from services.gateway_side.backup.names import DUMP_NAME_RE, REMOTE_ROOT, TS_FORMAT, stamp_utc


@pytest.mark.parametrize(
    ("name", "db", "stamp"),
    [
        ("ava-20260930T190000Z.dump.enc", "ava", "20260930T190000Z"),
        ("ava_main-20260930T190000Z.dump.enc", "ava_main", "20260930T190000Z"),
        ("my-db-20260930T190000Z.dump.enc", "my-db", "20260930T190000Z"),
        ("ava-20260609T190000Z.dump.gz.enc", "ava", "20260609T190000Z"),
        ("ava-20260609T190000Z.dump", "ava", "20260609T190000Z"),
    ],
)
def test_a_utc_stamped_dump_is_managed(name: str, db: str, stamp: str) -> None:
    match = DUMP_NAME_RE.fullmatch(name)

    assert match is not None
    assert (match["db"], match["ts"]) == (db, stamp)


@pytest.mark.parametrize(
    "name",
    [
        # Names no writer produces any more.
        "ava-20260827-030001.dump",  # pre-cutover wall-clock stamp, no offset
        "ava-20260827-030001.dump.gz.enc",
        "ava-20260930T190000Z.pre-update.dump.enc",
        "ava-20260930T190000Z.pitr-activation-11111111-1111-1111-1111-111111111111.dump.enc",
        # Never a dump.
        "manual.dump",
        "ava-before-migration.dump",
        "ava-20260930T190000Z.dump.partial",
        "ava-20260930T190000Z.dump.enc.partial",
        ".ava-20260930T190000Z.dump.enc.1.copy",
        "ava-20260930T190000.dump.enc",  # a stamp without its Z
        "20260930T190000Z.dump.enc",  # no database name
        "ava-20260930T190000Z.dump.enc.bak",
        "notes.txt",
    ],
)
def test_anything_else_is_left_alone(name: str) -> None:
    assert DUMP_NAME_RE.fullmatch(name) is None


def test_the_stamp_is_the_utc_instant_it_names() -> None:
    assert stamp_utc("20260930T190000Z") == datetime(2026, 9, 30, 19, 0, tzinfo=UTC)
    assert stamp_utc("20261101T093000Z") > stamp_utc("20261101T083000Z")
    assert datetime(2026, 9, 30, 19, 0, tzinfo=UTC).strftime(TS_FORMAT) == "20260930T190000Z"


def test_a_stamp_without_its_utc_marker_is_refused_not_guessed() -> None:
    with pytest.raises(ValueError, match="does not match format"):
        stamp_utc("20260827-030001")


def test_the_off_site_namespace_is_the_published_one() -> None:
    assert REMOTE_ROOT == "ava-logical"
