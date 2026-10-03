"""Boot owner-seed regression tests for the Feishu adapter (task #4930).

The owner open id is memory-only, and the daemon no longer replays user
messages across a restart, so the outbound leg went blind until the user's next
message (measured 6h+; only the daily patrol noticed). ``start()`` now seeds the
owner from the persisted switch state when exactly one feishu chat is recorded.
These tests pin the seed semantics: first-writer, no guessing on a missing /
unreadable / ambiguous state, and the telegram alert that keeps the blind
window observable instead of silent.

``send_to_owner`` plumbing is covered by ``test_feishu_adapter.py``; the send
seam used here only proves the seed resolves the owner.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from services.im_bridge import copy
from services.im_bridge.adapters.feishu import FeishuAdapter
from services.im_bridge.config import FeishuCredentialsConfig
from services.im_bridge.tests.slices import feishu_config
from services.im_bridge.types import InboundMessage


class FakeCore:
    """Adapter-side core double: inbound sink + the adapters registry."""

    def __init__(self) -> None:
        self.received: list[InboundMessage] = []
        self.adapters: dict[str, Any] = {}

    async def handle_inbound(self, message: InboundMessage) -> None:
        self.received.append(message)


class FakeTelegram:
    """Telegram stand-in recording ``send_to_owner`` calls."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    async def send_to_owner(self, text: str, **_: Any) -> None:
        if self.fail:
            raise RuntimeError("telegram unavailable")
        self.sent.append(text)


class BootAdapter(FeishuAdapter):
    """Adapter with the ws thread and the poller stubbed out: ``start()``'s seed
    is the only real work under test (no lark import, no network)."""

    def _run_ws(self) -> None:
        pass

    def _start_poller(self) -> None:
        pass


def _write_switch_state(tmp_path: Path, state: dict[str, int]) -> None:
    path = tmp_path / "state" / "im_bridge" / "switch_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


def _core_with_telegram(*, fail: bool = False) -> tuple[FakeCore, FakeTelegram]:
    core = FakeCore()
    telegram = FakeTelegram(fail=fail)
    core.adapters["telegram"] = telegram
    return core, telegram


def _boot_adapter(config: FeishuCredentialsConfig, core: FakeCore | None = None) -> BootAdapter:
    return BootAdapter(core or FakeCore(), config)


# -- boot path (start) ------------------------------------------------------


async def test_start_seeds_owner_open_id_from_switch_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The restart regression: with exactly one feishu chat recorded, boot
    restores the owner open id before the link goes live."""
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    _write_switch_state(tmp_path, {"feishu:ou_owner_1": 405, "telegram:12345": 405})
    core, telegram = _core_with_telegram()
    adapter = _boot_adapter(
        feishu_config(feishu_app_id="cli_x", feishu_app_secret="secret_x"),  # noqa: S106
        core,
    )

    await adapter.start()

    assert adapter._last_open_id == "ou_owner_1"
    assert telegram.sent == []


async def test_start_without_credentials_does_not_seed_or_alert(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A disabled feishu link (no credentials) has no outbound leg to rescue:
    no seed, no alert — the alert exists for a live-but-blind leg."""
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    _write_switch_state(tmp_path, {"feishu:ou_owner_1": 405})
    core, telegram = _core_with_telegram()
    adapter = _boot_adapter(feishu_config(feishu_app_id="", feishu_app_secret=""), core)

    await adapter.start()

    assert adapter._last_open_id == ""
    assert telegram.sent == []


# -- seed semantics ---------------------------------------------------------


async def test_seed_lets_send_to_owner_reach_the_seeded_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Positive case: after the seed, send_to_owner resolves the owner instead
    of raising 'no known user chat' (send call stubbed at the seam)."""
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    _write_switch_state(tmp_path, {"feishu:ou_owner_1": 405})
    adapter = FeishuAdapter(FakeCore(), feishu_config())
    await adapter._seed_owner_from_switch_state()

    sent: list[tuple[str, str]] = []

    async def fake_send(chat_id: str, text: str, **_: Any) -> None:
        sent.append((chat_id, text))

    monkeypatch.setattr(adapter, "send", fake_send)
    await adapter.send_to_owner("hi")

    assert sent == [("ou_owner_1", "hi")]


async def test_seed_never_overwrites_a_known_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """First-writer: an owner already known (e.g. from an inbound message) is
    never replaced by the persisted state — the rule _register_sent_chat keeps."""
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    _write_switch_state(tmp_path, {"feishu:ou_state": 405})
    core, telegram = _core_with_telegram()
    adapter = FeishuAdapter(core, feishu_config())
    adapter._last_open_id = "ou_known"

    await adapter._seed_owner_from_switch_state()

    assert adapter._last_open_id == "ou_known"
    assert telegram.sent == []


@pytest.mark.parametrize(
    "state",
    [None, {}, {"telegram:12345": 405}],
    ids=["missing-file", "empty-state", "no-feishu-key"],
)
async def test_seed_without_a_source_alerts_and_stays_blind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: dict[str, int] | None
) -> None:
    """No usable source (state missing / empty / no feishu chat) -> stay empty
    and alert the user through telegram, so the blind leg is observable."""
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    if state is not None:
        _write_switch_state(tmp_path, state)
    core, telegram = _core_with_telegram()
    adapter = FeishuAdapter(core, feishu_config())

    await adapter._seed_owner_from_switch_state()

    assert adapter._last_open_id == ""
    assert telegram.sent == [copy.FEISHU_OWNER_SEED_NO_SOURCE]


async def test_seed_with_multiple_feishu_chats_alerts_ambiguity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two recorded feishu chats are ambiguous: never guess one — alert instead."""
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    _write_switch_state(tmp_path, {"feishu:ou_a": 405, "feishu:ou_b": 405})
    core, telegram = _core_with_telegram()
    adapter = FeishuAdapter(core, feishu_config())

    await adapter._seed_owner_from_switch_state()

    assert adapter._last_open_id == ""
    assert telegram.sent == [copy.FEISHU_OWNER_SEED_AMBIGUOUS.format(count=2)]


async def test_seed_with_unreadable_state_alerts_and_stays_blind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A loader failure degrades to the old behavior (empty owner, no crash)
    and still alerts — the fallback must not be silent.

    The loader swallows OSError/ValueError by design; a valid-JSON non-object
    document is the corruption whose AttributeError escapes it.
    """
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    path = tmp_path / "state" / "im_bridge" / "switch_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")
    core, telegram = _core_with_telegram()
    adapter = FeishuAdapter(core, feishu_config())

    await adapter._seed_owner_from_switch_state()

    assert adapter._last_open_id == ""
    assert telegram.sent == [copy.FEISHU_OWNER_SEED_NO_SOURCE]


async def test_seed_alert_is_fail_soft(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A failing telegram leg, or none loaded at all, must never break boot."""
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    core, telegram = _core_with_telegram(fail=True)
    adapter = FeishuAdapter(core, feishu_config())

    await adapter._seed_owner_from_switch_state()  # telegram send raises: tolerated
    assert adapter._last_open_id == ""

    no_telegram = FeishuAdapter(FakeCore(), feishu_config())
    await no_telegram._seed_owner_from_switch_state()  # no telegram adapter: tolerated
    assert no_telegram._last_open_id == ""
    assert telegram.sent == []
