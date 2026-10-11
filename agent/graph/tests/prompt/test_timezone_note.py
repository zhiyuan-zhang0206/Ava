"""The cluster-timezone context note — the one place the agent is told which
timezone its timestamps are in.

Policy: the timestamps themselves carry no `%Z` suffix (see
`base/agents/messages/tests/test_envelope.py::TestWeekdayFlag::test_no_timezone_suffix_either_way`),
because `settings.general.timezone` is cluster-pinned — the suffix repeated one
constant on every stamp, and an ambiguous one. The declaration replaces it, and
these tests pin that it is rendered from the setting rather than hard-coded, so
a cluster on a different timezone is told its own.
"""

from __future__ import annotations

import pytest

from agent.graph.prompt.context_notes import RANK_CLUSTER_MEMORY, RANK_TIMEZONE, timezone_note
from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity
from base.agents.messages.kwargs import NoteTag
from base.clock import Clock
from base.config import settings
from base.host.env.agent_slices import AgentSlices
from base.lm.plugin_providers import build_model_catalog
from tests.fixtures.pin_agent import pin_agent, pin_no_identity


@pytest.fixture(autouse=True)
def _agent_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The note opts out without an established process identity, like every
    other framework note; give it one so the content is what is under test."""
    pin_agent(7)


def _context(agent_id: int | None = 7) -> AvaContext:
    return AvaContext(
        identity=AgentIdentity(agent_id=agent_id, owns_loop=True) if agent_id is not None else None,
        agent=AgentSlices.resolve(
            default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
        ),
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
    )


def _content(monkeypatch: pytest.MonkeyPatch, tz: str) -> str:
    monkeypatch.setattr(settings.general, "timezone", tz)
    note = timezone_note(_context())
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
    (`scripts/lint/note_tags.py` enforces the frontend half)."""
    monkeypatch.setattr(settings.general, "timezone", "Asia/Shanghai")
    note = timezone_note(_context())
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
    context = _context(None)
    pin_no_identity()
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    assert timezone_note(context) is None


def test_renders_under_a_hosted_turn_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The explicit host context works without binding the shared SDK."""

    context = _context(29)
    pin_no_identity()
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    monkeypatch.setattr(settings.general, "timezone", "Asia/Shanghai")
    note = timezone_note(context)

    assert note is not None
    assert "Asia/Shanghai" in str(note.content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]


def test_timezone_notes_follow_two_explicit_live_clock_roots() -> None:
    import os
    import time
    from unittest.mock import patch

    from base.clock import ClockConfig
    from base.config import ConfigBoot

    try:
        with patch.dict(os.environ):
            first, second = ConfigBoot(), ConfigBoot()
            first.set_field("timezone", "UTC")
            second.set_field("timezone", "Asia/Shanghai")

            def clock(owner: ConfigBoot) -> Clock:
                return Clock(
                    ClockConfig(
                        owner.view.general.timezone,
                        owner.view.general.timezone
                        if owner.field_explicitly_set("timezone")
                        else None,
                        owner.view.general.message_timestamp_weekday,
                    )
                )

            first_ctx = AvaContext(
                identity=AgentIdentity(1, True), clock_factory=lambda: clock(first)
            )
            second_ctx = AvaContext(
                identity=AgentIdentity(2, True), clock_factory=lambda: clock(second)
            )
            first_note, second_note = timezone_note(first_ctx), timezone_note(second_ctx)
            assert first_note is not None and second_note is not None
            assert "(UTC+00:00)" in str(first_note.content)
            assert "(UTC+08:00)" in str(second_note.content)
            first.set_field("timezone", "America/New_York")
            first_note, second_note = timezone_note(first_ctx), timezone_note(second_ctx)
            assert first_note is not None and second_note is not None
            assert "America/New_York" in str(first_note.content)
            assert "Asia/Shanghai" in str(second_note.content)
    finally:
        tzset = getattr(time, "tzset", None)
        if tzset is not None:
            tzset()
