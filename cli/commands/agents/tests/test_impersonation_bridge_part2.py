"""Push delivery with an ACK window: the relay delivers full inbox content
and retries an unacknowledged message once before ending the lease.

The relay never ACKs work itself and never renews the lease; a failed or
restarted relay cannot lose a pending message.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from cli.commands.agents import impersonation_relay as relay

LEASE_ID = UUID("767fb040-aa54-42ae-b2c8-594039fbbf46")
THREAD_ID = UUID("b9d32d0d-bd27-40fc-83e8-692769b21523")


def msg(
    message_id: int,
    *,
    kind: str = "system_note",
    source: str = "system",
    content: str | None = None,
) -> relay.InboxMessage:
    return relay.InboxMessage(
        id=message_id, kind=kind, source=source, content=content or f"body {message_id}"
    )


class Inbox:
    def __init__(self, *pending: int, page_size: int | None = None) -> None:
        self.messages: dict[int, relay.InboxMessage] = {i: msg(i) for i in pending}
        self.routine: set[int] = set(pending)
        self.batch_window = 0.0
        self.page_size = page_size
        self.status: relay.LeaseStatus = "active"
        self.expires_at = datetime.now(UTC) + timedelta(minutes=5)
        self.start_message = "Start here: resume the implementation from the failing test."
        self.reads = 0
        self.attempts: dict[int, tuple[int, float]] = {}
        self.ack_window_seconds = 180
        self.max_delivery_attempts = 2

    @property
    def pending(self) -> set[int]:
        return set(self.messages)

    def add(
        self, message_id: int, message: relay.InboxMessage | None = None, *, routine: bool = True
    ) -> None:
        self.messages[message_id] = message or msg(message_id)
        if routine:
            self.routine.add(message_id)
        else:
            self.routine.discard(message_id)

    def ack(self, *ids: int) -> None:
        for message_id in ids:
            self.messages.pop(message_id, None)
            self.routine.discard(message_id)

    async def reserve(self, ids: list[int]) -> frozenset[int]:
        for i in ids:
            count, _ = self.attempts.get(i, (0, 0))
            self.attempts[i] = (count + 1, relay._loop_time())
        return frozenset(ids)

    async def read(self) -> relay.InboxSnapshot:
        self.reads += 1
        if any(
            i in self.pending
            and count >= self.max_delivery_attempts
            and relay._loop_time() - at >= self.ack_window_seconds
            for i, (count, at) in self.attempts.items()
        ):
            self.status = "expired"
        page = frozenset(sorted(self.messages)[: self.page_size])
        return relay.InboxSnapshot(
            page,
            {
                i: replace(
                    self.messages[i],
                    delivery_attempts=self.attempts.get(i, (0, 0))[0],
                    delivery_due=i not in self.attempts
                    or relay._loop_time() - self.attempts[i][1] >= self.ack_window_seconds,
                )
                for i in page
            },
            self.expires_at,
            self.status,
            routine_ids=frozenset(i for i in page if i in self.routine),
            batch_window=self.batch_window,
            start_message=self.start_message,
            ack_window_seconds=self.ack_window_seconds,
            max_delivery_attempts=self.max_delivery_attempts,
        )

    @property
    def active(self) -> bool:
        return self.status == "active"

    @active.setter
    def active(self, value: bool) -> None:
        self.status = "active" if value else "released"


class Listener:
    def __init__(
        self,
        inbox: Inbox,
        *,
        subscribed: Callable[[], None] | None = None,
        waited: Callable[[int], None] | None = None,
    ) -> None:
        self.inbox = inbox
        self.subscribed = subscribed
        self.waited = waited
        self.waits: list[float] = []
        self.opened = False
        self.closed = False

    async def ensure_listening(self) -> None:
        self.opened = True
        if self.subscribed is not None:
            self.subscribed()

    async def wait_one(self, timeout: float) -> None:
        self.waits.append(timeout)
        if self.waited is None:
            self.inbox.active = False
        else:
            self.waited(len(self.waits))

    async def close(self) -> None:
        self.closed = True


class FakeClock:
    """Monotonic test clock patched onto relay._loop_time and relay.asyncio.sleep."""

    def __init__(self) -> None:
        self.t = 0.0

    def time(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(relay, "_loop_time", clock.time)

    async def fake_sleep(delay: float) -> None:
        clock.t += delay

    monkeypatch.setattr(relay.asyncio, "sleep", fake_sleep)
    return clock


@pytest.fixture(autouse=True)
def no_rate_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(relay, "_MIN_EMIT_INTERVAL_SECONDS", 0.0)


def run(
    inbox: Inbox,
    listener: Listener,
    emit: Callable[[str], None],
    *,
    debounce: float = 0.0,
    max_chars: int | None = None,
) -> None:
    asyncio.run(
        relay.relay_inbox(
            42,
            LEASE_ID,
            read_inbox=inbox.read,
            reserve=inbox.reserve,
            listener=listener,
            emit=emit,
            debounce=debounce,
            max_chars=max_chars,
        )
    )


# ── Start message and activation pushes ───────────────────────────────────────


# ── Merge window and urgency ───────────────────────────────────────────────────


# ── ACK window and re-delivery ─────────────────────────────────────────────────


# ── Failure, expiry and lifecycle ──────────────────────────────────────────────


# ── Emit rate and envelope bounds ──────────────────────────────────────────────


# ── Host emitters ──────────────────────────────────────────────────────────────


# ── Snapshot bridging ──────────────────────────────────────────────────────────


# ── Command plumbing ───────────────────────────────────────────────────────────


def test_two_missed_ack_windows_end_the_takeover_without_a_third_push(clock: FakeClock) -> None:
    inbox = Inbox(11)
    emitted: list[str] = []

    def waited(n: int) -> None:
        if n <= 2:
            clock.advance(inbox.ack_window_seconds + 1)
        else:
            inbox.active = False  # bound the old infinite-retry implementation

    listener = Listener(inbox, waited=waited)
    run(inbox, listener, emitted.append)

    assert sum("[id=11]" in text for text in emitted) == 2
    assert inbox.status == "expired"
    assert inbox.pending == {11}
    assert "Ava control expired" in emitted[-1]
    assert listener.closed
