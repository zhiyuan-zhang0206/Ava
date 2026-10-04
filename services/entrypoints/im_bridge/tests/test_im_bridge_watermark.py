"""Task #4933: the push watermark vs. a compact's item_id renumbering.

item_id is f"{msg_idx}.{block_idx}" — a POSITION in the session's message
list. A compact truncates that list, so the ids restart at small numbers while
a pre-compact watermark still names the old numbering: fresh became a
permanent empty set and the Telegram/Feishu pushes froze silently (2026-10-03,
10h/4h with zero log lines). The watermark now carries the item's created_at
(monotone, immune to renumbering) as its primary key, and a batch entirely
behind it is named and recovered instead of skipped in silence.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from services.entrypoints.im_bridge.core import IMBridgeCore
from services.entrypoints.im_bridge.cursor_store import PushWatermark
from services.entrypoints.im_bridge.tests.slices import gateway_client, im_bridge_config
from services.entrypoints.im_bridge.types import ChatState, IMAdapter


class _Adapter(IMAdapter):
    """Records sends; these tests only exercise the push path's `send`."""

    channel = "telegram"

    def __init__(self) -> None:
        super().__init__(core=None)
        self.sent: list[tuple[str, str]] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(
        self,
        chat_id: str,
        text: str,
        *,
        buttons: list[tuple[str, str]] | None = None,
        markdown: bool = False,
    ) -> None:
        del buttons, markdown
        self.sent.append((chat_id, text))


def _core() -> IMBridgeCore:
    """A core over an unconnected gateway client: `_push_snapshot` is driven
    directly and nothing here reaches the network."""

    return IMBridgeCore(im_bridge_config(), gateway_client())


def _agent_chat(item_id: str, payload: str, created_at: str | None = None) -> dict[str, Any]:
    """One agent text item in the gateway's timeline shape."""

    item: dict[str, Any] = {"kind": "agent_chat", "item_id": item_id, "payload": payload}
    if created_at is not None:
        item["created_at"] = created_at
    return item


def test_compact_renumbering_rollback_is_loud_and_self_healing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The 2026-10-03 incident shape: the whole batch sits behind the
    watermark (id-only row from before the upgrade). It must not return in
    silence — ERROR with channel/chat/agent/old/observed_max, the watermark
    reset to the observed max, the triggering batch skipped (no replay), and
    the push resumed on the next item."""
    caplog.set_level(logging.ERROR, logger="services.entrypoints.im_bridge.core")
    core = _core()
    adapter = _Adapter()
    core.register(adapter)
    state = ChatState("telegram", "12345")
    state.current_agent_id = 405
    core._last_pushed[("telegram", "12345", 405)] = PushWatermark(
        created_at=None,
        item_id="377.1",  # a row from before push_created_at existed
    )
    batch = [
        _agent_chat("1.0", "post-compact first", "2026-10-03T14:40:00+00:00"),
        _agent_chat("128.0", "agent tail", "2026-10-03T16:20:00+00:00"),
        _agent_chat("128.1", "agent newest", "2026-10-03T16:21:00+00:00"),
    ]

    async def scenario() -> None:
        await core._push_snapshot(("telegram", "12345"), state, {"items": list(batch)})
        assert adapter.sent == [], "the triggering batch is skipped, never replayed"
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1, "a rollback is named exactly once, not defaulted to silence"
        message = errors[0].getMessage()
        assert "telegram" in message and "12345" in message and "405" in message
        assert "377.1" in message and "128.1" in message
        # the reset lands on the observed max item (its stamp included)
        assert core._last_pushed[("telegram", "12345", 405)] == PushWatermark(
            created_at="2026-10-03T16:21:00+00:00", item_id="128.1"
        )
        # the next item flows again
        await core._push_snapshot(
            ("telegram", "12345"),
            state,
            {"items": [_agent_chat("129.0", "resumed", "2026-10-03T16:30:00+00:00")]},
        )
        assert adapter.sent == [("12345", "[Ava #405] resumed")]
        # and it does not re-fire on the same rollback (reset is idempotent)
        assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1

    asyncio.run(scenario())


def test_watermark_equality_is_not_a_rollback(caplog: pytest.LogCaptureFixture) -> None:
    """The benign shape: the re-observed tail sits exactly AT the watermark
    (the watermark item is the newest one). Only a strict step behind it is a
    rollback — with a <= rule every quiet reconnect would re-log and re-reset,
    and the watermark would never settle."""
    caplog.set_level(logging.ERROR, logger="services.entrypoints.im_bridge.core")
    core = _core()
    adapter = _Adapter()
    core.register(adapter)
    state = ChatState("telegram", "12345")
    state.current_agent_id = 405
    core._last_pushed[("telegram", "12345", 405)] = PushWatermark(
        created_at="2026-10-03T11:00:00+00:00", item_id="5.1"
    )

    async def scenario() -> None:
        batch = [
            _agent_chat("4.0", "older", "2026-10-03T10:00:00+00:00"),
            _agent_chat("5.1", "at the watermark", "2026-10-03T11:00:00+00:00"),
        ]
        await core._push_snapshot(("telegram", "12345"), state, {"items": batch})
        assert adapter.sent == []
        assert [r for r in caplog.records if r.levelno == logging.ERROR] == []
        assert core._last_pushed[("telegram", "12345", 405)] == PushWatermark(
            created_at="2026-10-03T11:00:00+00:00", item_id="5.1"
        )
        await core._push_snapshot(
            ("telegram", "12345"),
            state,
            {"items": [_agent_chat("6.0", "next", "2026-10-03T12:00:00+00:00")]},
        )
        assert adapter.sent == [("12345", "[Ava #405] next")]

    asyncio.run(scenario())


def test_empty_and_non_dialog_batches_are_noops(caplog: pytest.LogCaptureFixture) -> None:
    """Nothing to compare -> nothing to log or reset: an empty snapshot and a
    snapshot of non-dialog items only both leave the watermark alone."""
    caplog.set_level(logging.ERROR, logger="services.entrypoints.im_bridge.core")
    core = _core()
    state = ChatState("telegram", "12345")
    state.current_agent_id = 405
    core._last_pushed[("telegram", "12345", 405)] = PushWatermark(
        created_at="2026-10-03T12:00:00+00:00", item_id="9.0"
    )

    async def scenario() -> None:
        await core._push_snapshot(("telegram", "12345"), state, {"items": []})
        await core._push_snapshot(
            ("telegram", "12345"),
            state,
            {"items": [{"kind": "agent_code", "item_id": "2.0", "payload": "print(1)"}]},
        )
        assert [r for r in caplog.records if r.levelno == logging.ERROR] == []
        assert core._last_pushed[("telegram", "12345", 405)] == PushWatermark(
            created_at="2026-10-03T12:00:00+00:00", item_id="9.0"
        )

    asyncio.run(scenario())


def test_push_snapshot_survives_compact_renumbering(caplog: pytest.LogCaptureFixture) -> None:
    """The root fix itself: with created_at on the watermark, a post-compact
    batch — ids restarted small, stamps still rising — pushes straight
    through. No freeze, and nothing for the fallback to shout about."""
    caplog.set_level(logging.ERROR, logger="services.entrypoints.im_bridge.core")
    core = _core()
    adapter = _Adapter()
    core.register(adapter)
    state = ChatState("telegram", "12345")
    state.current_agent_id = 405
    # the pre-compact position: message 377's text block, stamped 14:33
    core._last_pushed[("telegram", "12345", 405)] = PushWatermark(
        created_at="2026-10-03T14:33:00+00:00", item_id="377.1"
    )

    async def scenario() -> None:
        await core._push_snapshot(
            ("telegram", "12345"),
            state,
            {
                "items": [
                    _agent_chat("1.0", "post-compact first", "2026-10-03T14:40:00+00:00"),
                    _agent_chat("2.0", "post-compact second", "2026-10-03T14:52:00+00:00"),
                ]
            },
        )
        assert adapter.sent == [
            ("12345", "[Ava #405] post-compact first"),
            ("12345", "[Ava #405] post-compact second"),
        ]
        assert core._last_pushed[("telegram", "12345", 405)] == PushWatermark(
            created_at="2026-10-03T14:52:00+00:00", item_id="2.0"
        )
        assert [r for r in caplog.records if r.levelno == logging.ERROR] == []

    asyncio.run(scenario())


def test_created_at_orders_fresh_items_over_item_id(caplog: pytest.LogCaptureFixture) -> None:
    """created_at decides freshness, item_id only orders ties: in one batch,
    an item whose id is numerically above the watermark but stamped earlier
    stays out, and the renumbered item stamped later gets pushed — the exact
    reversal of the old id-only rule that froze the push."""
    caplog.set_level(logging.ERROR, logger="services.entrypoints.im_bridge.core")
    core = _core()
    adapter = _Adapter()
    core.register(adapter)
    state = ChatState("telegram", "12345")
    state.current_agent_id = 405
    core._last_pushed[("telegram", "12345", 405)] = PushWatermark(
        created_at="2026-10-03T14:00:00+00:00", item_id="100.0"
    )

    async def scenario() -> None:
        await core._push_snapshot(
            ("telegram", "12345"),
            state,
            {
                "items": [
                    _agent_chat("101.0", "stamped earlier", "2026-10-03T13:00:00+00:00"),
                    _agent_chat("1.0", "renumbered later", "2026-10-03T15:00:00+00:00"),
                ]
            },
        )
        assert adapter.sent == [("12345", "[Ava #405] renumbered later")]
        assert core._last_pushed[("telegram", "12345", 405)] == PushWatermark(
            created_at="2026-10-03T15:00:00+00:00", item_id="1.0"
        )
        assert [r for r in caplog.records if r.levelno == logging.ERROR] == []

    asyncio.run(scenario())


def test_stamped_batch_behind_the_watermark_is_not_a_rollback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stamped watermark can explain a fresh-empty batch on its own: every
    item is OLDER IN TIME (readable created_at <= the watermark's), so the
    item ids are merely small — a compact wiped the session's positions and
    this chat has not produced anything new yet. The numbering check must not
    run at all: no ERROR, no reset, nothing pushed. (Review delta vs
    c672f181f: running it on item_id alone fired one spurious ERROR + reset
    per compact per chat while snapshots carried only the wiped tail.)"""

    caplog.set_level(logging.ERROR, logger="services.entrypoints.im_bridge.core")
    core = _core()
    adapter = _Adapter()
    core.register(adapter)
    state = ChatState("telegram", "12345")
    state.current_agent_id = 405
    core._last_pushed[("telegram", "12345", 405)] = PushWatermark(
        created_at="2026-10-03T14:33:00+00:00", item_id="377.1"
    )

    async def scenario() -> None:
        await core._push_snapshot(
            ("telegram", "12345"),
            state,
            {
                "items": [
                    _agent_chat("1.0", "wiped tail a", "2026-10-03T14:00:00+00:00"),
                    _agent_chat("2.0", "wiped tail b", "2026-10-03T14:20:00+00:00"),
                ]
            },
        )
        assert adapter.sent == []
        assert [r for r in caplog.records if r.levelno == logging.ERROR] == []
        assert core._last_pushed[("telegram", "12345", 405)] == PushWatermark(
            created_at="2026-10-03T14:33:00+00:00",
            item_id="377.1",  # untouched
        )
        # a genuinely newer item flows as usual, and the pair advances
        await core._push_snapshot(
            ("telegram", "12345"),
            state,
            {"items": [_agent_chat("3.0", "post-compact news", "2026-10-03T14:40:00+00:00")]},
        )
        assert adapter.sent == [("12345", "[Ava #405] post-compact news")]
        assert core._last_pushed[("telegram", "12345", 405)] == PushWatermark(
            created_at="2026-10-03T14:40:00+00:00", item_id="3.0"
        )

    asyncio.run(scenario())


def test_numbering_check_still_runs_for_a_stamp_less_batch_max(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the batch max carries no stamp the clock cannot decide, so the
    numbering check stands even against a stamped watermark: a max strictly
    behind it is a rollback — loud, reset to the observed max, batch
    skipped — and the next item resumes."""
    caplog.set_level(logging.ERROR, logger="services.entrypoints.im_bridge.core")
    core = _core()
    adapter = _Adapter()
    core.register(adapter)
    state = ChatState("telegram", "12345")
    state.current_agent_id = 405
    core._last_pushed[("telegram", "12345", 405)] = PushWatermark(
        created_at="2026-10-03T14:33:00+00:00", item_id="377.1"
    )

    async def scenario() -> None:
        await core._push_snapshot(
            ("telegram", "12345"),
            state,
            {"items": [_agent_chat("100.0", "legacy tail with no stamp")]},
        )
        assert adapter.sent == []
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "377.1" in errors[0].getMessage()
        assert core._last_pushed[("telegram", "12345", 405)] == PushWatermark(
            created_at=None, item_id="100.0"
        )
        await core._push_snapshot(
            ("telegram", "12345"),
            state,
            {"items": [_agent_chat("101.0", "resumed after reset", "2026-10-03T14:50:00+00:00")]},
        )
        assert adapter.sent == [("12345", "[Ava #405] resumed after reset")]

    asyncio.run(scenario())
