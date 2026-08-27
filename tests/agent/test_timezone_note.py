"""The cluster-timezone context note — the one place the agent is told which
timezone its timestamps are in.

Policy: the timestamps themselves carry no `%Z` suffix (see
`tests/agent/test_envelope.py::TestWeekdayFlag::test_no_timezone_suffix_either_way`),
because `settings.general.timezone` is cluster-pinned — the suffix repeated one
constant on every stamp, and an ambiguous one. The declaration replaces it, and
these tests pin that it is rendered from the setting rather than hard-coded, so
a cluster on a different timezone is told its own.
"""

from __future__ import annotations

import pytest

from agent.graph._context_notes import RANK_CLUSTER_MEMORY, RANK_TIMEZONE, timezone_note
from shared.config import settings
from shared.message_kwargs import NoteTag


@pytest.fixture(autouse=True)
def _agent_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The note opts out without an established process identity, like every
    other framework note; give it one so the content is what is under test."""
    monkeypatch.setattr("ava._boot._agent_id", 7)


def _content(monkeypatch: pytest.MonkeyPatch, tz: str) -> str:
    monkeypatch.setattr(settings.general, "timezone", tz)
    note = timezone_note()
    assert note is not None
    return str(note.content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]


def test_declares_the_configured_zone_by_iana_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """The IANA name, not the `%Z` abbreviation — `CST` names both US Central
    and China Standard time, `Asia/Shanghai` names one zone."""
    assert "Asia/Shanghai" in _content(monkeypatch, "Asia/Shanghai")


def test_offset_is_rendered_from_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two clusters, two different declarations — nothing here is hard-coded to
    the default zone. Shanghai has no DST so its offset is stable year-round;
    the Los Angeles case asserts only the shape, since it moves with DST."""
    import re

    assert "(UTC+08:00)" in _content(monkeypatch, "Asia/Shanghai")
    assert "(UTC+00:00)" in _content(monkeypatch, "UTC")
    la = _content(monkeypatch, "America/Los_Angeles")
    assert re.search(r"\(UTC-0[78]:00\)", la), la


def test_carries_the_timezone_note_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tag drives the UI chip; an unmapped one renders as a loud alarm
    (`scripts/lint_note_tags.py` enforces the frontend half)."""
    monkeypatch.setattr(settings.general, "timezone", "Asia/Shanghai")
    note = timezone_note()
    assert note is not None
    assert note.additional_kwargs["ava_note_tag"] == NoteTag.TIMEZONE  # pyright: ignore[reportUnknownMemberType]


def test_sits_in_the_stable_cache_band(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ahead of the shared memory index, and that is a cache decision: the
    index is re-read at every window establishment and changes whenever any
    agent writes memory, so a note behind it re-caches on someone else's write.
    The declaration changes only when `AVA_TIMEZONE` does — which already
    forces an agent restart."""
    assert RANK_TIMEZONE < RANK_CLUSTER_MEMORY


def test_opts_out_without_an_agent_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Snapshot renders and the dev REPL have no identity; the note declines
    rather than producing a head fragment out of context."""
    monkeypatch.setattr("ava._boot._agent_id", None)
    assert timezone_note() is None
