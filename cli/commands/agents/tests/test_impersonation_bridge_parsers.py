"""`ava impersonate relay` parses its arguments and carries the codex remote and the content cap down to the relay."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from base.agents.impersonation.status import ImpersonationStatus
from base.events.live import redis_listener
from cli.commands.agents import impersonation_adapters as adapters
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
        self.status: ImpersonationStatus = ImpersonationStatus.ACTIVE
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
            self.status = ImpersonationStatus.EXPIRED
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
        return self.status == ImpersonationStatus.ACTIVE

    @active.setter
    def active(self, value: bool) -> None:
        self.status = ImpersonationStatus.ACTIVE if value else ImpersonationStatus.RELEASED


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


def _serve_inbox(monkeypatch: pytest.MonkeyPatch, inbox: Inbox) -> None:
    """Serve the fake inbox through the public relay calls `cmd_relay` reads it with."""

    def lease(_db: object, _bus: object, *_args: object) -> dict[str, Any]:
        return {
            "session_id": 0,
            "id": str(LEASE_ID),
            "agent_id": 42,
            "status": inbox.status,
            "expires_at": inbox.expires_at,
            "ack_window_seconds": inbox.ack_window_seconds,
            "max_delivery_attempts": inbox.max_delivery_attempts,
            "relay_batch_window_seconds": inbox.batch_window,
            "start_message": inbox.start_message,
        }

    def rows(_db: object, *_args: object) -> list[dict[str, Any]]:
        return [
            {
                "id": i,
                "kind": inbox.messages[i].kind,
                "source": inbox.messages[i].source,
                "content": inbox.messages[i].content,
                "delivery_attempts": inbox.messages[i].delivery_attempts,
                "delivery_due": inbox.messages[i].delivery_due,
            }
            for i in sorted(inbox.messages)[: inbox.page_size]
        ]

    monkeypatch.setattr("base.agents.impersonation.relay_get", lease)
    monkeypatch.setattr("base.agents.impersonation.relay_inbox", rows)

    def beat(_db: object, *_args: object) -> None:
        return None

    monkeypatch.setattr("base.agents.impersonation.relay_heartbeat", beat)


def test_command_passes_remote_to_steer(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands.agents import impersonation
    from cli.parsers import build_parser

    remote = "unix:///private/tmp/ava-codex.sock"
    args = build_parser().parse_args(
        [
            "impersonate",
            "relay",
            "42",
            "--lease-id",
            str(LEASE_ID),
            "--provider",
            "codex",
            "--thread-id",
            str(THREAD_ID),
            "--codex-remote",
            remote,
            "--debounce",
            "0",
        ]
    )
    inbox = Inbox()
    listener = Listener(inbox)
    delivered: list[tuple[UUID, str | None]] = []
    monkeypatch.setattr(impersonation, "relay_token_from_env", lambda: "test-credential")
    inbox.start_message = ""
    _serve_inbox(monkeypatch, inbox)

    def make_listener(*_args: object) -> Listener:
        return listener

    def reserve(
        _db: object, _bus: object, _lease: str, _token: str, ids: list[int]
    ) -> frozenset[int]:
        for i in ids:
            inbox.messages[i] = replace(inbox.messages[i], delivery_attempts=1, delivery_due=False)
        return frozenset(ids)

    monkeypatch.setattr(relay, "reserve_delivery", reserve)
    monkeypatch.setattr(redis_listener, "RedisInboundListener", make_listener)

    def deliver(thread_id: str, _message: str, *, endpoint: str) -> None:
        delivered.append((UUID(thread_id), endpoint))

    monkeypatch.setattr(adapters, "live_submit", deliver)
    assert args.func(args) == 0
    assert delivered == [(THREAD_ID, remote)]
    assert listener.closed


@pytest.mark.parametrize("refuse", [False, True])
def test_codex_relay_caps_content_and_preserves_inbox_on_steer_failure(
    monkeypatch: pytest.MonkeyPatch,
    refuse: bool,
) -> None:
    """Bound host context and leave failed messages for the native handoff."""
    from cli.commands.agents import impersonation
    from cli.parsers import build_parser

    args = build_parser().parse_args(
        [
            "impersonate",
            "relay",
            "42",
            "--lease-id",
            str(LEASE_ID),
            "--provider",
            "codex",
            "--thread-id",
            str(THREAD_ID),
            "--debounce",
            "0",
        ]
    )
    inbox = Inbox(5)
    inbox.messages[5] = msg(5, content="x" * 5000)
    listener = Listener(inbox)
    delivered: list[str] = []
    monkeypatch.setattr(impersonation, "relay_token_from_env", lambda: "test-credential")
    _serve_inbox(monkeypatch, inbox)

    def make_listener(*_args: object) -> Listener:
        return listener

    def reserve(
        _db: object, _bus: object, _lease: str, _token: str, ids: list[int]
    ) -> frozenset[int]:
        for i in ids:
            inbox.messages[i] = replace(inbox.messages[i], delivery_attempts=1, delivery_due=False)
        return frozenset(ids)

    monkeypatch.setattr(relay, "reserve_delivery", reserve)
    monkeypatch.setattr(redis_listener, "RedisInboundListener", make_listener)

    def deliver(_thread_id: str, message: str, *, endpoint: str) -> str | None:
        delivered.append(message)
        if refuse and "Ava message" in message:
            return "ActiveTurnNotSteerable"
        return None

    monkeypatch.setattr(adapters, "live_submit", deliver)
    args.codex_remote = "unix:///tmp/ava-codex.sock"
    assert args.func(args) == (1 if refuse else 0)
    if refuse:
        assert 5 in inbox.messages
    push = delivered[-1]
    assert "truncated" in push
    assert "before acknowledging" in push
    assert relay.ack_command(0, [5], agent_id=42) in push
    body_line = [line for line in push.splitlines() if line.startswith("x" * 10)]
    assert body_line
    assert len(body_line[0]) <= relay._PUSH_MAX_CHARS + len(
        " (truncated — run the inbox command to read the full body before acknowledging)"
    )
    assert listener.closed
