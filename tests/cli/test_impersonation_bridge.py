"""Push delivery with an ACK window: the relay delivers full inbox content
and retries an unacknowledged message once before ending the lease.

The relay never ACKs work itself and never renews the lease; a failed or
restarted relay cannot lose a pending message.
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from cli.commands import impersonation_relay as relay


def _public_relay_session(*_args: object, **_kwargs: object) -> dict[str, int]:
    return {"session_id": 0}


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


def test_start_message_then_pending_push_skips_the_merge_window() -> None:
    inbox = Inbox(5, 6)
    inbox.batch_window = 30.0  # routine rows would wait; activation rows skip it
    emitted: list[str] = []

    def emit(text: str) -> None:
        emitted.append(text)

    run(inbox, Listener(inbox), emit)

    assert len(emitted) == 2
    assert emitted[0] == inbox.start_message
    push = emitted[1]
    assert "ids=5,6" in push
    assert "[id=5] kind=system_note from=system" in push
    assert "body 5" in push and "body 6" in push
    assert relay.ack_command(LEASE_ID, [5, 6]) in push
    assert "re-delivery" not in push


def test_empty_start_message_falls_back_to_the_activation_hint() -> None:
    inbox = Inbox()
    inbox.start_message = ""
    emitted: list[str] = []
    run(inbox, Listener(inbox), emitted.append)
    assert len(emitted) == 1
    assert "Ava control active" in emitted[0]
    ack = shlex.join([sys.executable, "-m", "cli", "impersonate", "ack", str(LEASE_ID)])
    inbox_cmd = shlex.join([sys.executable, "-m", "cli", "impersonate", "inbox", str(LEASE_ID)])
    assert ack in emitted[0]
    assert inbox_cmd in emitted[0]
    assert "agents timeline" not in emitted[0]


def test_subscription_gap_is_reconciled_before_waiting() -> None:
    inbox = Inbox()
    listener = Listener(inbox, subscribed=lambda: inbox.add(7))
    emitted: list[str] = []

    def emit(text: str) -> None:
        assert listener.opened
        assert listener.waits == []
        emitted.append(text)

    run(inbox, listener, emit)

    assert emitted[0] == inbox.start_message
    assert len(emitted) == 2
    assert "ids=7" in emitted[1]
    assert inbox.pending == {7}
    assert listener.closed


def test_native_catchup_recovers_message_with_no_redis_publish() -> None:
    inbox = Inbox()

    def waited(n: int) -> None:
        if n == 1:
            inbox.add(8)
        else:
            inbox.active = False

    emitted: list[str] = []
    run(inbox, Listener(inbox, waited=waited), emitted.append)

    assert len(emitted) == 2
    assert emitted[0] == inbox.start_message
    assert "ids=8" in emitted[1]


def test_waits_for_native_consent_then_delivers_start_and_inbox() -> None:
    inbox = Inbox(7)
    inbox.status = "requested"

    def waited(n: int) -> None:
        if n == 1:
            inbox.status = "active"
        else:
            inbox.active = False

    emitted: list[str] = []
    run(inbox, Listener(inbox, waited=waited), emitted.append)

    assert emitted[0] == inbox.start_message
    assert len(emitted) == 2
    assert "ids=7" in emitted[1]


# ── Merge window and urgency ───────────────────────────────────────────────────


def test_fresh_routine_arrival_waits_out_the_merge_window(clock: FakeClock) -> None:
    inbox = Inbox()
    inbox.batch_window = 30.0
    emitted: list[str] = []

    def waited(n: int) -> None:
        if n == 1:
            inbox.add(8)
            clock.advance(29.0)  # window still open
        elif n == 2:
            clock.advance(30.0)  # window elapsed (59s > 30s deadline from t=29)
        else:
            inbox.active = False

    run(inbox, Listener(inbox, waited=waited), emitted.append)

    assert emitted[0] == inbox.start_message
    assert len(emitted) == 2
    assert "ids=8" in emitted[1]


@pytest.mark.parametrize(
    ("kind", "source"),
    [("chat", "user"), ("cancel", "system"), ("reminder", "system")],
)
def test_urgent_arrivals_push_immediately_without_the_merge_window(
    clock: FakeClock, kind: str, source: str
) -> None:
    inbox = Inbox()
    inbox.batch_window = 30.0
    emitted: list[str] = []

    def waited(n: int) -> None:
        if n == 1:
            inbox.add(9, msg(9, kind=kind, source=source), routine=False)
        else:
            inbox.active = False

    run(inbox, Listener(inbox, waited=waited), emitted.append)

    assert len(emitted) == 2
    assert emitted[0] == inbox.start_message
    assert "ids=9" in emitted[1]
    assert f"[id=9] kind={kind} from={source}" in emitted[1]


# ── ACK window and re-delivery ─────────────────────────────────────────────────


def test_unacknowledged_batch_is_redelivered_after_the_ack_window(clock: FakeClock) -> None:
    inbox = Inbox(11)
    emitted: list[str] = []

    def waited(n: int) -> None:
        if n == 1:
            clock.advance(inbox.ack_window_seconds + 1)
        else:
            inbox.active = False

    run(inbox, Listener(inbox, waited=waited), emitted.append)

    assert len(emitted) == 3
    assert emitted[0] == inbox.start_message
    assert "ids=11" in emitted[1]
    assert "re-delivery" not in emitted[1]
    assert "re-delivery: unacknowledged" in emitted[2]
    assert "ids=11" in emitted[2]


def test_redelivery_covers_only_still_unacknowledged_ids(clock: FakeClock) -> None:
    inbox = Inbox(11, 12)
    emitted: list[str] = []

    def waited(n: int) -> None:
        if n == 1:
            inbox.ack(11)  # half processed
            clock.advance(inbox.ack_window_seconds + 1)
        else:
            inbox.active = False

    run(inbox, Listener(inbox, waited=waited), emitted.append)

    assert len(emitted) == 3
    assert "ids=11,12" in emitted[1]
    assert "ids=12" in emitted[2]
    assert "[id=11]" not in emitted[2]  # the re-pushed page is {12} only
    assert "re-delivery: unacknowledged" in emitted[2]


def test_redelivery_stops_when_the_host_acks(clock: FakeClock) -> None:
    inbox = Inbox(11, 12)
    emitted: list[str] = []

    def waited(n: int) -> None:
        if n == 1:
            inbox.ack(11, 12)
            clock.advance(inbox.ack_window_seconds + 1)
        else:
            inbox.active = False

    run(inbox, Listener(inbox, waited=waited), emitted.append)

    assert len(emitted) == 2
    assert emitted[0] == inbox.start_message
    assert "ids=11,12" in emitted[1]
    assert all("re-delivery" not in text for text in emitted)


def test_new_message_under_an_outstanding_batch_is_pushed_as_a_new_batch() -> None:
    inbox = Inbox(5)
    emitted: list[str] = []

    def waited(n: int) -> None:
        if n == 1:
            inbox.add(6)  # no ACK: batch one stays outstanding
        else:
            inbox.active = False

    run(inbox, Listener(inbox, waited=waited), emitted.append)

    assert len(emitted) == 3
    assert emitted[0] == inbox.start_message
    assert "ids=5" in emitted[1]
    assert "ids=6" in emitted[2]


# ── Failure, expiry and lifecycle ──────────────────────────────────────────────


def test_failed_delivery_preserves_pending_and_restart_replays() -> None:
    inbox = Inbox(11)
    first_listener = Listener(inbox)

    def fail(_message: str) -> None:
        raise RuntimeError("host offline")

    with pytest.raises(RuntimeError, match="host offline"):
        run(inbox, first_listener, fail)
    assert inbox.pending == {11}
    assert first_listener.closed
    assert first_listener.waits == []

    emitted: list[str] = []
    run(inbox, Listener(inbox), emitted.append)
    assert len(emitted) == 2
    assert emitted[0] == inbox.start_message
    assert "ids=11" in emitted[1]
    assert inbox.pending == {11}


def test_release_during_debounce_prevents_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    inbox = Inbox(11)

    async def release(_delay: float) -> None:
        inbox.active = False

    monkeypatch.setattr(relay.asyncio, "sleep", release)
    emitted: list[str] = []
    run(inbox, Listener(inbox), emitted.append, debounce=1.0)
    assert emitted == [inbox.start_message]


def test_expired_lease_notifies_host_without_subscribing_or_extending() -> None:
    inbox = Inbox()
    inbox.status = "expired"
    listener = Listener(inbox)
    emitted: list[str] = []
    run(inbox, listener, emitted.append)
    assert len(emitted) == 1
    assert "control expired" in emitted[0]
    assert not listener.opened


def test_active_expiry_notifies_loss_of_control() -> None:
    inbox = Inbox()

    def waited(n: int) -> None:
        if n == 1:
            inbox.status = "expired"

    emitted: list[str] = []
    run(inbox, Listener(inbox, waited=waited), emitted.append)
    assert len(emitted) == 2
    assert emitted[0] == inbox.start_message
    assert "control expired" in emitted[1]


@pytest.mark.parametrize("outcome", ["released", "rejected", "expired"])
def test_waiting_controller_is_told_terminal_outcome(outcome: relay.LeaseStatus) -> None:
    inbox = Inbox()

    def waited(n: int) -> None:
        inbox.status = outcome

    emitted: list[str] = []
    run(inbox, Listener(inbox, waited=waited), emitted.append)

    if outcome == "released":
        assert emitted == [inbox.start_message]
    else:
        assert len(emitted) == 2
        assert emitted[0] == inbox.start_message
        assert f"control {outcome}" in emitted[1]


def test_wait_never_extends_beyond_lease_lifetime() -> None:
    inbox = Inbox(5)
    inbox.expires_at = datetime.now(UTC) + timedelta(seconds=8)

    def waited(n: int) -> None:
        if n == 1:
            inbox.active = False

    listener = Listener(inbox, waited=waited)
    emitted: list[str] = []
    run(inbox, listener, emitted.append)
    assert len(emitted) == 2  # start message + the activation-pending push
    assert listener.waits
    assert all(timeout <= 8.0 for timeout in listener.waits)


def test_local_clock_cannot_revoke_db_active_lease_or_busy_spin() -> None:
    inbox = Inbox()
    inbox.expires_at = datetime.now(UTC) - timedelta(minutes=5)
    listener = Listener(inbox)
    emitted: list[str] = []
    run(inbox, listener, emitted.append)
    assert len(emitted) == 1
    assert emitted[0] == inbox.start_message
    assert listener.waits == [0.5]


# ── Emit rate and envelope bounds ──────────────────────────────────────────────


def test_emits_respect_the_monitor_event_rate_across_ack_cycles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(relay, "_MIN_EMIT_INTERVAL_SECONDS", 2.0)
    inbox = Inbox(1)
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 2:
            inbox.add(3)  # bursts merge inside the debounce window

    def waited(n: int) -> None:
        if n == 1:
            inbox.ack(1)
            inbox.add(2)
        else:
            inbox.active = False

    monkeypatch.setattr(relay.asyncio, "sleep", sleep)
    emitted: list[str] = []
    run(inbox, Listener(inbox, waited=waited), emitted.append)

    assert len(emitted) == 3
    assert emitted[0] == inbox.start_message
    assert "ids=1" in emitted[1]
    assert "ids=2,3" in emitted[2]
    assert 1.9 <= delays[1] <= 2.0


def test_claude_envelope_truncates_long_content_but_keeps_the_ack_line() -> None:
    inbox = Inbox(1)
    inbox.messages[1] = msg(1, content="x" * 5000)
    emitted: list[str] = []
    run(inbox, Listener(inbox), emitted.append, max_chars=relay._PUSH_MAX_CHARS)

    assert len(emitted) == 2
    push = emitted[1]
    assert "truncated" in push
    assert relay.ack_command(LEASE_ID, [1]) in push
    body_line = [line for line in push.splitlines() if line.startswith("x" * 10)]
    assert body_line
    assert len(body_line[0]) <= relay._PUSH_MAX_CHARS + len(
        " (truncated; run the inbox command to read the full message)"
    )


# ── Host emitters ──────────────────────────────────────────────────────────────


def test_codex_requires_a_control_endpoint_without_queueing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.commands import codex_app_server

    monkeypatch.setattr(codex_app_server, "default_control_endpoint", lambda: None)
    with pytest.raises(RuntimeError, match=r"Steer.*--codex-remote"):
        relay.host_emitter("codex", str(THREAD_ID))


@pytest.mark.parametrize("failure", ["ActiveTurnNotSteerable", "TimeoutError", "unknown thread"])
def test_codex_refusal_never_falls_back_to_pending(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from unittest.mock import Mock

    queued = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr("shared.proc.run_bounded", queued)

    def refuse(_thread_id: str, _message: str, *, endpoint: str) -> str:
        return failure

    monkeypatch.setattr(relay, "live_submit", refuse)
    emit = relay.host_emitter("codex", str(THREAD_ID), codex_remote="unix:///tmp/ava-codex.sock")
    with pytest.raises(RuntimeError, match="Steer delivery failed"):
        emit("push")
    queued.assert_not_called()


@pytest.mark.parametrize("explicit", [True, False])
def test_codex_emitter_delivers_literal_input_to_the_owning_server(
    monkeypatch: pytest.MonkeyPatch, explicit: bool
) -> None:
    from cli.commands import codex_app_server

    attempts: list[tuple[str, str, str]] = []
    endpoint = "unix:///tmp/ava-codex.sock"
    message = "Ava push with literal $(no-shell) and `no-shell`"

    def delivered_live(thread_id: str, text: str, *, endpoint: str) -> None:
        attempts.append((thread_id, text, endpoint))

    monkeypatch.setattr(codex_app_server, "default_control_endpoint", lambda: endpoint)
    monkeypatch.setattr(relay, "live_submit", delivered_live)
    relay.host_emitter("codex", str(THREAD_ID), codex_remote=endpoint if explicit else None)(
        message
    )
    assert attempts == [(str(THREAD_ID), message, endpoint)]


def test_claude_rejects_codex_remote() -> None:
    with pytest.raises(ValueError, match="--codex-remote"):
        relay.host_emitter("claude", None, codex_remote="unix:///tmp/codex.sock")


@pytest.mark.parametrize("provider,thread_id", [("codex", None), ("claude", "thread"), ("?", None)])
def test_host_target_must_be_explicit(provider: str, thread_id: str | None) -> None:
    with pytest.raises(ValueError):
        relay.host_emitter(provider, thread_id)


# ── Snapshot bridging ──────────────────────────────────────────────────────────


def test_shared_inbox_rows_keep_their_bodies_for_the_push_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared import impersonation

    lease: dict[str, Any] = {
        "id": str(LEASE_ID),
        "agent_id": 42,
        "status": "active",
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "ack_window_seconds": 180,
        "max_delivery_attempts": 2,
    }
    calls: list[tuple[str, str]] = []

    def get(lease_id: str, token: str) -> dict[str, Any]:
        calls.append((lease_id, token))
        return lease

    def inbox(lease_id: str, token: str) -> list[dict[str, Any]]:
        calls.append((lease_id, token))
        return [
            {
                "id": 7,
                "kind": "chat",
                "source": "user",
                "content": "the body rides into the push envelope",
                "payload": None,
                "created_at": datetime.now(UTC),
                "delivery_attempts": 0,
                "delivery_due": True,
            }
        ]

    monkeypatch.setattr(impersonation, "relay_get", get)
    monkeypatch.setattr(impersonation, "relay_inbox", inbox)
    snapshot = relay._read_inbox(42, LEASE_ID, "memory-only-token")
    assert snapshot.message_ids == frozenset({7})
    assert snapshot.messages[7].content == "the body rides into the push envelope"
    assert snapshot.start_message == ""
    assert calls == [(str(LEASE_ID), "memory-only-token")] * 2


def test_agent_mismatch_refuses_inbox_before_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared import impersonation

    def get(_lease_id: str, _token: str) -> dict[str, Any]:
        return {
            "id": str(LEASE_ID),
            "agent_id": 99,
            "status": "active",
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
            "ack_window_seconds": 180,
            "max_delivery_attempts": 2,
        }

    def inbox(_lease_id: str, _token: str) -> list[dict[str, Any]]:
        pytest.fail("Agent mismatch must not read this inbox")

    monkeypatch.setattr(impersonation, "relay_get", get)
    monkeypatch.setattr(impersonation, "relay_inbox", inbox)
    with pytest.raises(ValueError, match="does not belong"):
        relay._read_inbox(42, LEASE_ID, "memory-only-token")


# ── Command plumbing ───────────────────────────────────────────────────────────


def test_command_passes_remote_to_steer(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands import impersonation
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
    monkeypatch.setattr("shared.impersonation.relay_get", _public_relay_session)

    def read(*_args: object) -> relay.InboxSnapshot:
        return relay.InboxSnapshot(frozenset(), {}, inbox.expires_at, inbox.status)

    def make_listener(*_args: object) -> Listener:
        return listener

    monkeypatch.setattr(relay, "_read_inbox", read)

    def reserve(_lease: str, _token: str, ids: list[int]) -> frozenset[int]:
        for i in ids:
            inbox.messages[i] = replace(inbox.messages[i], delivery_attempts=1, delivery_due=False)
        return frozenset(ids)

    monkeypatch.setattr(relay, "reserve_delivery", reserve)
    monkeypatch.setattr(relay.shared.redis_listener, "RedisInboundListener", make_listener)

    def heartbeat_ok(_lease_id: UUID, _token: str) -> bool:
        return True

    async def heartbeat_loop(_lease_id: UUID, _token: str, **kwargs: float) -> None:
        _ = kwargs
        await asyncio.sleep(0)

    monkeypatch.setattr(relay, "_write_heartbeat", heartbeat_ok)
    monkeypatch.setattr(relay, "_heartbeat_loop", heartbeat_loop)

    def deliver(thread_id: str, _message: str, *, endpoint: str) -> None:
        delivered.append((UUID(thread_id), endpoint))

    monkeypatch.setattr(relay, "live_submit", deliver)
    assert args.func(args) == 0
    assert delivered == [(THREAD_ID, remote)]
    assert listener.closed


@pytest.mark.parametrize("refuse", [False, True])
def test_codex_relay_caps_content_and_preserves_inbox_on_steer_failure(
    monkeypatch: pytest.MonkeyPatch,
    refuse: bool,
) -> None:
    """Bound host context and leave failed messages for the native handoff."""
    from cli.commands import impersonation
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
    monkeypatch.setattr("shared.impersonation.relay_get", _public_relay_session)

    def read(*_args: object) -> relay.InboxSnapshot:
        page = frozenset(sorted(inbox.messages)[: inbox.page_size])
        return relay.InboxSnapshot(
            page,
            {i: inbox.messages[i] for i in page},
            inbox.expires_at,
            inbox.status,
            routine_ids=frozenset(i for i in page if i in inbox.routine),
            batch_window=inbox.batch_window,
            start_message=inbox.start_message,
        )

    def make_listener(*_args: object) -> Listener:
        return listener

    monkeypatch.setattr(relay, "_read_inbox", read)

    def reserve(_lease: str, _token: str, ids: list[int]) -> frozenset[int]:
        for i in ids:
            inbox.messages[i] = replace(inbox.messages[i], delivery_attempts=1, delivery_due=False)
        return frozenset(ids)

    monkeypatch.setattr(relay, "reserve_delivery", reserve)
    monkeypatch.setattr(relay.shared.redis_listener, "RedisInboundListener", make_listener)

    def heartbeat_ok(_lease_id: UUID, _token: str) -> bool:
        return True

    async def heartbeat_loop(_lease_id: UUID, _token: str, **kwargs: float) -> None:
        _ = kwargs
        await asyncio.sleep(0)

    monkeypatch.setattr(relay, "_write_heartbeat", heartbeat_ok)
    monkeypatch.setattr(relay, "_heartbeat_loop", heartbeat_loop)

    def deliver(_thread_id: str, message: str, *, endpoint: str) -> str | None:
        delivered.append(message)
        if refuse and "Ava message" in message:
            return "ActiveTurnNotSteerable"
        return None

    monkeypatch.setattr(relay, "live_submit", deliver)
    args.codex_remote = "unix:///tmp/ava-codex.sock"
    assert args.func(args) == (1 if refuse else 0)
    if refuse:
        assert 5 in inbox.messages
    push = delivered[-1]
    assert "truncated" in push
    assert relay.ack_command(0, [5], agent_id=42) in push
    body_line = [line for line in push.splitlines() if line.startswith("x" * 10)]
    assert body_line
    assert len(body_line[0]) <= relay._PUSH_MAX_CHARS + len(
        " (truncated; run the inbox command to read the full message)"
    )
    assert listener.closed


def test_write_heartbeat_stops_at_a_terminal_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock

    from shared import impersonation as leases

    beat = Mock()
    monkeypatch.setattr("shared.impersonation.relay_heartbeat", beat)
    assert relay._write_heartbeat(LEASE_ID, "relay-token") is True
    beat.assert_called_once_with(str(LEASE_ID), "relay-token")
    beat.side_effect = leases.ImpersonationError("Impersonation has ended")
    assert relay._write_heartbeat(LEASE_ID, "relay-token") is False


@pytest.mark.parametrize("debounce", [-1.0, 31.0, float("nan"), float("inf")])
def test_invalid_debounce_fails_before_open(debounce: float) -> None:
    inbox = Inbox()
    listener = Listener(inbox)
    with pytest.raises(ValueError, match="debounce"):
        asyncio.run(
            relay.relay_inbox(
                42,
                LEASE_ID,
                read_inbox=inbox.read,
                reserve=inbox.reserve,
                listener=listener,
                emit=lambda _push: None,
                debounce=debounce,
            )
        )
    assert not listener.opened


def test_release_racing_with_read_stops_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared import impersonation

    states = iter(["active", "released"])

    def get(_lease_id: str, _token: str) -> dict[str, Any]:
        return {
            "id": str(LEASE_ID),
            "agent_id": 42,
            "status": next(states),
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
            "ack_window_seconds": 180,
            "max_delivery_attempts": 2,
        }

    def inbox(_lease_id: str, _token: str) -> list[dict[str, Any]]:
        raise impersonation.ImpersonationError("Lease released concurrently")

    monkeypatch.setattr(impersonation, "relay_get", get)
    monkeypatch.setattr(impersonation, "relay_inbox", inbox)
    assert not relay._read_inbox(42, LEASE_ID, "test-token").active


@pytest.mark.parametrize("status", ["requested", "accepted"])
def test_pending_consent_checks_status_without_opening_inbox(
    monkeypatch: pytest.MonkeyPatch, status: relay.LeaseStatus
) -> None:
    from shared import impersonation

    def get(_lease_id: str, _token: str) -> dict[str, Any]:
        return {
            "id": str(LEASE_ID),
            "agent_id": 42,
            "status": status,
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
            "ack_window_seconds": 180,
            "max_delivery_attempts": 2,
        }

    def inbox(_lease_id: str, _token: str) -> list[dict[str, Any]]:
        pytest.fail("Pending consent must not read the protected inbox")

    monkeypatch.setattr(impersonation, "relay_get", get)
    monkeypatch.setattr(impersonation, "relay_inbox", inbox)
    snapshot = relay._read_inbox(42, LEASE_ID, "test-token")
    assert snapshot.status == status
    assert not snapshot.message_ids


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
