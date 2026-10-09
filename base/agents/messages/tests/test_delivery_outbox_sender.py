"""Deferred-delivery sender keys, durable journal records and content normalization."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from base.agents.messages import delivery_outbox as outbox
from base.config import Settings
from base.config.service_read import ConfigAuthority

_NOW = datetime(2026, 9, 17, 9, 30, 0, tzinfo=UTC)


def _limits(**overrides: object) -> outbox.DeliveryOutboxLimits:
    base: dict[str, object] = {
        "enabled": True,
        "retry_backoff_steps": (30.0, 60.0, 300.0, 900.0),
        "budget_seconds": 43200.0,
        "abandoned_retention_days": 30,
        "dedup_window_seconds": 900.0,
        "flush_interval_seconds": 30.0,
        "max_entries": 128,
    }
    base.update(overrides)
    return outbox.DeliveryOutboxLimits(**base)  # type: ignore[arg-type]


@pytest.fixture()
def authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ConfigAuthority]:
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    outbox._reset_caches_for_tests()
    runtime = Settings(profile=None)
    yield ConfigAuthority(runtime=runtime, all_domains=runtime, env_path=tmp_path / ".env")
    outbox._reset_caches_for_tests()


def _patch_limits(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    snapshot = _limits(**overrides)

    def read_limits(_authority: ConfigAuthority) -> outbox.DeliveryOutboxLimits:
        return snapshot

    monkeypatch.setattr(outbox, "limits", read_limits)
    outbox._reset_caches_for_tests()


def _record(
    authority: ConfigAuthority,
    *,
    agent_id: int,
    source: str = "watcher:7",
    content: str = "the daily check fired",
    key: str = "key-1",
    now: datetime | None = None,
) -> Path | None:
    return outbox.record_failed_send(
        authority=authority,
        origin_agent_id=None,
        agent_id=agent_id,
        source=source,
        content=content,
        client_message_id=key,
        now=now,
    )


def test_record_merges_same_message_within_window(authority: ConfigAuthority) -> None:
    """Two failures of the same (target, source, content) inside the window
    are one logical message: one file, attempt count folded, key advanced."""
    first = _record(authority, agent_id=7, key="key-1")
    second = _record(authority, agent_id=7, key="key-2")
    assert first is not None and second == first
    assert [p.name for p in outbox.journal_dir().glob("*.json")] == [first.name]
    entry = outbox._read(first)
    assert entry is not None and entry.state == "pending"
    assert entry.attempts == 2
    assert entry.client_message_id == "key-2"
    other = _record(authority, agent_id=7, content="a different fire")
    assert other is not None and other != first


def test_read_accepts_legacy_fingerprint_without_completion_metadata(
    authority: ConfigAuthority,
) -> None:
    """Old pending entries remain replayable when their metadata is absent."""
    content = "the daily check fired"
    legacy_raw = f"7\x1fwatcher:7\x1f{outbox._canonical_content(content)}"
    legacy_fingerprint = hashlib.sha256(legacy_raw.encode("utf-8")).hexdigest()[:16]
    entry = outbox.OutboxEntry(
        schema_version=1,
        agent_id=7,
        source="watcher:7",
        content=content,
        client_message_id="legacy-key",
        created_at=_NOW.isoformat(),
        last_attempt_at=_NOW.isoformat(),
        attempts=1,
        origin_agent_id=None,
        origin_pid=None,
        flush_attempts=0,
        last_flush_at=None,
        state=outbox.DeliveryOutboxState.PENDING,
        abandon_reason=None,
        abandon_detail=None,
        abandoned_at=None,
    )
    path = outbox.journal_dir() / outbox._entry_path_name(7, legacy_fingerprint, _NOW)
    outbox._write_atomic(path, entry)

    assert outbox._read(path) == entry


def test_record_splits_messages_further_apart_than_window(authority: ConfigAuthority) -> None:
    """Identical content after the window is a new logical message."""
    first = _record(authority, agent_id=7, now=_NOW)
    second = _record(authority, agent_id=7, now=_NOW + timedelta(seconds=1000))
    assert first is not None and second is not None and first != second
    assert len(list(outbox.journal_dir().glob("*.json"))) == 2


def test_record_refused_when_disabled(
    authority: ConfigAuthority, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_limits(monkeypatch, enabled=False)
    assert _record(authority, agent_id=7) is None
    assert not list(outbox.journal_dir().glob("*.json"))


def test_record_refused_at_entry_cap(
    authority: ConfigAuthority, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_limits(monkeypatch, max_entries=1)
    assert _record(authority, agent_id=7, key="key-1") is not None
    assert _record(authority, agent_id=7, content="second message", key="key-2") is None
    assert len(list(outbox.journal_dir().glob("*.json"))) == 1


def test_logical_key_reuses_until_delivery_then_rotates(
    authority: ConfigAuthority, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_limits(monkeypatch)
    sender = outbox.DeliverySenderConfig(authority)
    first = outbox.logical_key(sender=sender, agent_id=7, source="watcher:7", content="check")
    assert (
        outbox.logical_key(sender=sender, agent_id=7, source="watcher:7", content="check") == first
    )
    outbox.retire_send(agent_id=7, source="watcher:7", content="check", key=first)
    assert (
        outbox.logical_key(sender=sender, agent_id=7, source="watcher:7", content="check") != first
    )
    assert (
        outbox.logical_key(sender=sender, agent_id=7, source="watcher:7", content="other") != first
    )


def test_retire_send_retires_only_the_matching_record(
    authority: ConfigAuthority, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_limits(monkeypatch)
    path = _record(authority, agent_id=7, content="check", key="key-1")
    assert path is not None
    # A different attempt's key must not retire this record.
    outbox.retire_send(agent_id=7, source="watcher:7", content="check", key="key-other")
    assert path.exists()
    outbox.retire_send(agent_id=7, source="watcher:7", content="check", key="key-1")
    assert not path.exists()


def test_split_content_matches_the_route_normalization() -> None:
    assert outbox.split_content("plain") == ("plain", None)
    # Wire strings are stripped by `_MessageContent` before the row is written
    # (strip_whitespace=True); the twin must match, or a whitespace-edged
    # replay of an already-committed key looks like a different message
    # (a false key_conflict/409).
    assert outbox.split_content("  padded\n") == ("padded", None)
    blocks: list[dict[str, object]] = [
        {"type": "text", "text": "look ma"},
        {"type": "image_url", "image_url": {"url": "https://x/i.png"}},
    ]
    text, payload = outbox.split_content(blocks)
    assert text == "look ma"
    assert payload == {"content_blocks": blocks}
    assert outbox.split_content([{"type": "image_url", "image_url": {"url": "u"}}]) == (
        "[image]",
        {"content_blocks": [{"type": "image_url", "image_url": {"url": "u"}}]},
    )
