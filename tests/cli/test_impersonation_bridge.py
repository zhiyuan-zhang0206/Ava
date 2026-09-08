"""Push delivery with an ACK window: the relay delivers full inbox content
and re-delivers unacknowledged batches until the host ACKs or the lease ends.

The relay never ACKs work itself and never renews the lease; a failed or
restarted relay cannot lose a pending message.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from cli.commands import impersonation_relay as relay

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

    async def read(self) -> relay.InboxSnapshot:
        self.reads += 1
        page = frozenset(sorted(self.messages)[: self.page_size])
        return relay.InboxSnapshot(
            page,
            {i: self.messages[i] for i in page},
            self.expires_at,
            self.status,
            routine_ids=frozenset(i for i in page if i in self.routine),
            batch_window=self.batch_window,
            start_message=self.start_message,
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
            clock.advance(relay._ACK_WINDOW_SECONDS + 1)
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
            clock.advance(relay._ACK_WINDOW_SECONDS + 1)
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
            clock.advance(relay._ACK_WINDOW_SECONDS + 1)
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
    run(inbox, Listener(inbox), emitted.append, max_chars=relay._CLAUDE_MAX_CHARS)

    assert len(emitted) == 2
    push = emitted[1]
    assert "truncated" in push
    assert relay.ack_command(LEASE_ID, [1]) in push
    body_line = [line for line in push.splitlines() if line.startswith("x" * 10)]
    assert body_line
    assert len(body_line[0]) <= relay._CLAUDE_MAX_CHARS + len(
        " (truncated; run the inbox command to read the full message)"
    )


# ── Host emitters ──────────────────────────────────────────────────────────────


def test_codex_queues_exact_thread_and_literal_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex"
    executable.write_text(
        f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n"
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    real_run = relay.shared.proc.run_bounded
    outputs: list[subprocess.CompletedProcess[str]] = []

    def record(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        result = real_run(argv, **kwargs)  # type: ignore[arg-type]
        outputs.append(result)
        return result

    monkeypatch.setattr(relay.shared.proc, "run_bounded", record)
    message = "Ava push with literal $(no-shell) and `no-shell`"
    relay.host_emitter("codex", str(THREAD_ID))(message)
    assert len(outputs) == 1
    assert json.loads(outputs[0].stdout) == [
        "queue",
        "--thread",
        str(THREAD_ID),
        "--message",
        message,
    ]


def test_codex_queue_honours_the_remote_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex"
    executable.write_text(
        f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n"
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    real_run = relay.shared.proc.run_bounded
    outputs: list[subprocess.CompletedProcess[str]] = []

    def record(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        result = real_run(argv, **kwargs)  # type: ignore[arg-type]
        outputs.append(result)
        return result

    monkeypatch.setattr(relay.shared.proc, "run_bounded", record)
    remote = "unix:///private/tmp/ava-codex.sock"
    relay.host_emitter("codex", str(THREAD_ID), codex_remote=remote)("push")
    assert json.loads(outputs[0].stdout)[-2:] == ["--remote", remote]


def test_claude_rejects_codex_remote() -> None:
    with pytest.raises(ValueError, match="--codex-remote"):
        relay.host_emitter("claude", None, codex_remote="unix:///tmp/codex.sock")


@pytest.mark.parametrize("provider,thread_id", [("codex", None), ("claude", "thread"), ("?", None)])
def test_host_target_must_be_explicit(provider: str, thread_id: str | None) -> None:
    with pytest.raises(ValueError):
        relay.host_emitter(provider, thread_id)


def test_codex_failure_is_not_delivery_or_provider_output_leak(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(_argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 7, "sensitive provider stdout", "sensitive stderr")

    monkeypatch.setattr(relay.shared.proc, "run_bounded", fail)
    with pytest.raises(RuntimeError, match="exit code 7"):
        relay.queue_codex(THREAD_ID, "test push")
    assert capsys.readouterr() == ("", "")


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
        }

    def inbox(_lease_id: str, _token: str) -> list[dict[str, Any]]:
        pytest.fail("Agent mismatch must not read this inbox")

    monkeypatch.setattr(impersonation, "relay_get", get)
    monkeypatch.setattr(impersonation, "relay_inbox", inbox)
    with pytest.raises(ValueError, match="does not belong"):
        relay._read_inbox(42, LEASE_ID, "memory-only-token")


# ── Command plumbing ───────────────────────────────────────────────────────────


def test_command_passes_remote_to_queue(monkeypatch: pytest.MonkeyPatch) -> None:
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
    queued: list[tuple[UUID, str | None]] = []
    monkeypatch.setattr(impersonation, "relay_token_from_env", lambda: "test-credential")

    def read(*_args: object) -> relay.InboxSnapshot:
        return relay.InboxSnapshot(frozenset(), {}, inbox.expires_at, inbox.status)

    def make_listener(*_args: object) -> Listener:
        return listener

    monkeypatch.setattr(relay, "_read_inbox", read)
    monkeypatch.setattr(relay.shared.redis_listener, "RedisInboundListener", make_listener)

    def heartbeat_ok(_lease_id: UUID, _token: str) -> bool:
        return True

    async def heartbeat_loop(_lease_id: UUID, _token: str, **kwargs: float) -> None:
        _ = kwargs
        await asyncio.sleep(0)

    monkeypatch.setattr(relay, "_write_heartbeat", heartbeat_ok)
    monkeypatch.setattr(relay, "_heartbeat_loop", heartbeat_loop)

    def queue(thread_id: UUID, _message: str, *, remote: str | None = None) -> None:
        queued.append((thread_id, remote))

    monkeypatch.setattr(relay, "queue_codex", queue)
    assert args.func(args) == 0
    assert queued == [(THREAD_ID, remote)]
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
        }

    def inbox(_lease_id: str, _token: str) -> list[dict[str, Any]]:
        pytest.fail("Pending consent must not read the protected inbox")

    monkeypatch.setattr(impersonation, "relay_get", get)
    monkeypatch.setattr(impersonation, "relay_inbox", inbox)
    snapshot = relay._read_inbox(42, LEASE_ID, "test-token")
    assert snapshot.status == status
    assert not snapshot.message_ids
