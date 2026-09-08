"""The host wake is advisory: a failed or restarted relay cannot ACK work."""

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
from unittest.mock import Mock
from uuid import UUID

import pytest

from cli.commands import impersonation_relay as relay

LEASE_ID = UUID("767fb040-aa54-42ae-b2c8-594039fbbf46")
THREAD_ID = UUID("b9d32d0d-bd27-40fc-83e8-692769b21523")


class Inbox:
    def __init__(self, *pending: int, page_size: int | None = None) -> None:
        self.pending: set[int] = set(pending)
        self.routine: set[int] = set()
        self.batch_window = 0.0
        self.page_size = page_size
        self.status: relay.LeaseStatus = "active"
        self.expires_at = datetime.now(UTC) + timedelta(minutes=5)
        self.reads = 0

    async def read(self) -> relay.InboxSnapshot:
        self.reads += 1
        page = frozenset(sorted(self.pending)[: self.page_size])
        return relay.InboxSnapshot(
            page,
            self.expires_at,
            self.status,
            routine_ids=frozenset(sorted(self.routine)[: self.page_size]),
            batch_window=self.batch_window,
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


class SleepingListener:
    """Like Listener, but actually sleeps (bounded) so real-time windows elapse."""

    def __init__(
        self,
        inbox: Inbox,
        *,
        waited: Callable[[int], None] | None = None,
        sleep_cap: float = 0.03,
    ) -> None:
        self.inbox = inbox
        self.waited = waited
        self.sleep_cap = sleep_cap
        self.waits: list[float] = []
        self.closed = False

    async def ensure_listening(self) -> None:
        pass

    async def wait_one(self, timeout: float) -> None:
        self.waits.append(timeout)
        if self.waited is not None:
            self.waited(len(self.waits))
        await asyncio.sleep(min(timeout, self.sleep_cap))

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def no_rate_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(relay, "_MIN_HINT_INTERVAL_SECONDS", 0.0)


def run(inbox: Inbox, listener: Listener, emit: Callable[[str], None]) -> None:
    asyncio.run(
        relay.relay_inbox(
            42,
            LEASE_ID,
            read_inbox=inbox.read,
            listener=listener,
            emit=emit,
            debounce=0,
        )
    )


def test_subscription_gap_is_reconciled_before_waiting() -> None:
    inbox = Inbox()
    listener = Listener(inbox, subscribed=lambda: inbox.pending.add(7))
    hints: list[str] = []

    def emit(hint: str) -> None:
        assert listener.opened
        assert listener.waits == []
        hints.append(hint)

    run(inbox, listener, emit)

    assert len(hints) == 1
    assert "newest_id=7" in hints[0]
    assert inbox.pending == {7}
    assert listener.closed


def test_native_catchup_recovers_message_with_no_redis_publish() -> None:
    inbox = Inbox()

    def waited(n: int) -> None:
        if n == 1:
            inbox.pending.add(8)
        else:
            inbox.active = False

    listener = Listener(inbox, waited=waited)
    hints: list[str] = []
    run(inbox, listener, hints.append)

    assert listener.waits == [30.0, 30.0]
    assert len(hints) == 2
    assert "control active" in hints[0]
    assert "newest_id=8" in hints[1]
    assert inbox.pending == {8}


def test_repeated_wakes_do_not_repeat_unacknowledged_hint() -> None:
    inbox = Inbox(7)

    def waited(n: int) -> None:
        if n == 4:
            inbox.active = False

    listener = Listener(inbox, waited=waited)
    hints: list[str] = []
    run(inbox, listener, hints.append)

    assert len(hints) == 1
    assert len(listener.waits) == 4
    assert inbox.pending == {7}


def test_ack_reveals_a_lower_id_without_a_new_message() -> None:
    # The host ACKed 10; a later-committed lower id 9 must surface on the next
    # wake even though nothing new arrived. Outstanding tracking follows the
    # pending set, so an ACK shrinks it and the residual page becomes hintable.
    inbox = Inbox(10)

    def waited(n: int) -> None:
        if n == 1:
            inbox.pending.remove(10)  # controller ACKed the hinted message
            inbox.pending.add(9)  # a lower id committed later
        else:
            inbox.active = False

    hints: list[str] = []
    run(inbox, Listener(inbox, waited=waited), hints.append)

    assert len(hints) == 2
    assert "newest_id=10" in hints[0]
    assert "newest_id=9" in hints[1]
    assert inbox.pending == {9}


def test_ack_wake_exposes_next_inbox_page_without_a_new_message() -> None:
    inbox = Inbox(*range(1, 102), page_size=100)

    def waited(n: int) -> None:
        if n == 1:
            # The controller's ACK publishes a wake after removing page one.
            inbox.pending.difference_update(range(1, 101))
        else:
            inbox.active = False

    hints: list[str] = []
    run(inbox, Listener(inbox, waited=waited), hints.append)

    assert len(hints) == 2
    assert "pending_page=100 newest_id=100" in hints[0]
    assert "pending_page=1 newest_id=101" in hints[1]
    assert inbox.pending == {101}


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

    hints: list[str] = []
    run(inbox, Listener(inbox), hints.append)
    assert len(hints) == 1
    assert inbox.pending == {11}


def test_release_during_debounce_prevents_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    inbox = Inbox(12)

    async def release(_delay: float) -> None:
        inbox.active = False

    monkeypatch.setattr(relay.asyncio, "sleep", release)
    listener = Listener(inbox)
    hints: list[str] = []
    run(inbox, listener, hints.append)

    assert hints == []
    assert listener.waits == []
    assert inbox.pending == {12}
    assert listener.closed


def test_expired_lease_notifies_host_without_subscribing_or_extending() -> None:
    inbox = Inbox(13)
    deadline = datetime.now(UTC) - timedelta(seconds=1)
    inbox.expires_at = deadline
    inbox.status = "expired"
    listener = Listener(inbox)
    hints: list[str] = []
    run(inbox, listener, hints.append)

    assert not listener.opened
    assert listener.closed
    assert len(hints) == 1
    assert "control expired" in hints[0]
    assert inbox.expires_at == deadline


def test_wait_never_extends_beyond_lease_lifetime() -> None:
    inbox = Inbox()
    inbox.expires_at = datetime.now(UTC) + timedelta(seconds=3)
    listener = Listener(inbox)
    run(inbox, listener, lambda _hint: None)
    assert len(listener.waits) == 1
    assert 0 < listener.waits[0] <= 3


def test_waits_for_native_consent_then_wakes_empty_active_inbox() -> None:
    inbox = Inbox()
    inbox.status = "requested"
    phases: list[relay.LeaseStatus] = ["accepted", "active", "active", "released"]

    def waited(n: int) -> None:
        inbox.status = phases[n - 1]

    listener = Listener(inbox, waited=waited)
    hints: list[str] = []

    def emit(hint: str) -> None:
        assert len(listener.waits) == 2
        hints.append(hint)

    run(inbox, listener, emit)
    assert len(hints) == 1
    assert "control active" in hints[0]
    assert "Read missing context" in hints[0]
    assert "pending_page=0" in hints[0]
    assert len(listener.waits) == 4


@pytest.mark.parametrize("outcome", ["rejected", "expired"])
def test_waiting_controller_is_told_terminal_outcome(outcome: relay.LeaseStatus) -> None:
    inbox = Inbox()
    inbox.status = "requested"

    def waited(_n: int) -> None:
        inbox.status = outcome

    hints: list[str] = []
    run(inbox, Listener(inbox, waited=waited), hints.append)
    assert len(hints) == 1
    assert f"control {outcome}" in hints[0]
    assert "Do not use this identity" in hints[0]


def test_active_expiry_notifies_loss_of_control() -> None:
    inbox = Inbox()

    def waited(_n: int) -> None:
        inbox.status = "expired"

    hints: list[str] = []
    run(inbox, Listener(inbox, waited=waited), hints.append)
    assert len(hints) == 2
    assert "control active" in hints[0]
    assert "control expired" in hints[1]


def test_local_clock_cannot_revoke_db_active_lease_or_busy_spin() -> None:
    inbox = Inbox()
    inbox.expires_at = datetime.now(UTC) - timedelta(minutes=5)
    listener = Listener(inbox)
    hints: list[str] = []
    run(inbox, listener, hints.append)
    assert len(hints) == 1
    assert "control active" in hints[0]
    assert listener.waits == [0.5]


def test_hints_respect_the_monitor_event_rate_across_ack_cycles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(relay, "_MIN_HINT_INTERVAL_SECONDS", 2.0)
    inbox = Inbox(1)
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 2:
            inbox.pending.add(3)

    def waited(n: int) -> None:
        if n == 1:
            inbox.pending.remove(1)  # ACK cycle closes the outstanding hint
            inbox.pending.add(2)  # ... and new work arrives immediately
        else:
            inbox.active = False

    monkeypatch.setattr(relay.asyncio, "sleep", sleep)
    hints: list[str] = []
    run(inbox, Listener(inbox, waited=waited), hints.append)

    assert len(hints) == 2
    assert "newest_id=1" in hints[0]
    assert "newest_id=3" in hints[1]
    assert 1.9 <= delays[1] <= 2.0


def test_new_message_under_an_outstanding_hint_does_not_queue_another() -> None:
    inbox = Inbox(5)

    def waited(n: int) -> None:
        if n == 1:
            inbox.pending.add(6)  # no ACK: the host has not processed hint one
        else:
            inbox.active = False

    hints: list[str] = []
    run(inbox, Listener(inbox, waited=waited), hints.append)

    assert len(hints) == 1
    assert "newest_id=5" in hints[0]


def test_outstanding_hint_is_never_replayed() -> None:
    # The host never ACKs: the pending page stays outstanding across many
    # wakes, and the relay must not re-queue the same hint (user ruling:
    # delivered hints invalidate, no replay).
    inbox = Inbox(5)

    def waited(n: int) -> None:
        if n >= 4:
            inbox.active = False

    hints: list[str] = []
    run(inbox, Listener(inbox, waited=waited), hints.append)

    assert len(hints) == 1
    assert "newest_id=5" in hints[0]


def test_unprocessed_outstanding_hint_logs_a_stall_instead_of_replaying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(relay, "_HINT_UNPROCESSED_ALERT_SECONDS", 0.0)
    errors: list[tuple[str, dict[str, object]]] = []

    class FakeLogger:
        def error(self, message: str, **fields: object) -> None:
            errors.append((message, fields))

    monkeypatch.setattr("shared.log.logger", FakeLogger())
    inbox = Inbox(5)

    def waited(n: int) -> None:
        if n >= 3:
            inbox.active = False

    hints: list[str] = []
    run(inbox, Listener(inbox, waited=waited), hints.append)

    assert len(hints) == 1
    assert len(errors) == 1
    assert "not replayed" in errors[0][0]
    assert errors[0][1]["outstanding_ids"] == [5]


@pytest.mark.parametrize(("pending", "newest"), [(frozenset[int](), 0), (frozenset({1, 2, 3}), 3)])
def test_claude_writes_one_short_line(
    capsys: pytest.CaptureFixture[str], pending: frozenset[int], newest: int
) -> None:
    message = relay.inbox_hint(42, LEASE_ID, pending)
    relay.monitor_claude(message)
    captured = capsys.readouterr()
    assert captured.out == message + "\n"
    assert len(message) < 500
    assert captured.err == ""
    assert "explicitly ACK" in message
    assert f"pending_page={len(pending)} newest_id={newest}" in message
    assert len(relay.activation_hint(42, LEASE_ID, frozenset())) < 500


def test_activation_hint_provides_independent_inbox_and_ack_commands() -> None:
    hint = relay.activation_hint(42, LEASE_ID, frozenset())
    for verb in ("inbox", "ack"):
        command = shlex.join([sys.executable, "-m", "cli", "impersonate", verb, str(LEASE_ID)])
        assert command in hint
    assert "agents timeline" not in hint
    assert "same CLI" not in hint


@pytest.mark.parametrize("remote", [None, "unix:///private/tmp/Ava queue $(literal).sock"])
def test_codex_queues_exact_thread_and_literal_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote: str | None
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
    message = "Ava hint with literal $(no-shell) and `no-shell`"
    relay.host_emitter("codex", str(THREAD_ID), codex_remote=remote)(message)
    assert len(outputs) == 1
    assert json.loads(outputs[0].stdout) == [
        "queue",
        "--thread",
        str(THREAD_ID),
        "--message",
        message,
    ] + (["--remote", remote] if remote is not None else [])


def test_claude_rejects_codex_remote() -> None:
    with pytest.raises(ValueError, match="--codex-remote"):
        relay.host_emitter("claude", None, codex_remote="unix:///tmp/codex.sock")


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
        return relay.InboxSnapshot(frozenset(), inbox.expires_at, inbox.status)

    def make_listener(*_args: object) -> Listener:
        return listener

    monkeypatch.setattr(relay, "_read_inbox", read)
    monkeypatch.setattr(relay.shared.redis_listener, "RedisInboundListener", make_listener)

    def heartbeat_ok(_lease_id: UUID, _token: str) -> bool:
        return True

    async def heartbeat_loop(_lease_id: UUID, _token: str, **kwargs: float) -> None:
        _ = kwargs
        await asyncio.sleep(0)

    def no_invalidation(_thread_id: UUID, _agent_id: int, _lease_id: UUID) -> int:
        return 0

    monkeypatch.setattr(relay, "_write_heartbeat", heartbeat_ok)
    monkeypatch.setattr(relay, "_heartbeat_loop", heartbeat_loop)
    monkeypatch.setattr(relay, "invalidate_our_hints", no_invalidation)

    def queue(thread_id: UUID, _message: str, *, remote: str | None = None) -> None:
        queued.append((thread_id, remote))

    monkeypatch.setattr(relay, "queue_codex", queue)
    assert args.func(args) == 0
    assert queued == [(THREAD_ID, remote)]
    assert listener.closed


def test_codex_failure_is_not_delivery_or_provider_output_leak(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(_argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 7, "sensitive provider stdout", "sensitive stderr")

    monkeypatch.setattr(relay.shared.proc, "run_bounded", fail)
    with pytest.raises(RuntimeError, match="exit code 7"):
        relay.queue_codex(THREAD_ID, "test hint")
    assert capsys.readouterr() == ("", "")


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
                emit=lambda _hint: None,
                debounce=debounce,
            )
        )
    assert not listener.opened


def test_shared_inbox_uses_lease_and_drops_message_bodies(
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
                "content": "message body must not enter host hints",
            }
        ]

    monkeypatch.setattr(impersonation, "relay_get", get)
    monkeypatch.setattr(impersonation, "relay_inbox", inbox)
    snapshot = relay._read_inbox(42, LEASE_ID, "memory-only-token")
    assert snapshot.message_ids == frozenset({7})
    assert "message body" not in repr(snapshot)
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
        relay._read_inbox(42, LEASE_ID, "test-token")


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


@pytest.mark.parametrize(
    ("provider", "thread_id"),
    [("codex", None), ("codex", "last"), ("claude", str(THREAD_ID)), ("other", None)],
)
def test_host_target_must_be_explicit(provider: str, thread_id: str | None) -> None:
    with pytest.raises(ValueError):
        relay.host_emitter(provider, thread_id)


# ── Relay heartbeat ──────────────────────────────────────────────────────────


def test_write_heartbeat_stops_at_a_terminal_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared import impersonation as leases

    beat = Mock()
    monkeypatch.setattr("shared.impersonation.relay_heartbeat", beat)
    assert relay._write_heartbeat(LEASE_ID, "relay-token") is True
    beat.assert_called_once_with(str(LEASE_ID), "relay-token")
    beat.side_effect = leases.ImpersonationError("Impersonation has ended")
    assert relay._write_heartbeat(LEASE_ID, "relay-token") is False


def test_heartbeat_loop_stops_when_the_lease_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    beat = Mock(return_value=False)
    monkeypatch.setattr(relay, "_write_heartbeat", beat)

    async def run() -> None:
        await relay._heartbeat_loop(LEASE_ID, "relay-token", interval=0)

    asyncio.run(run())
    beat.assert_called_once_with(LEASE_ID, "relay-token")


# ── Host queue invalidation (official RPC, no private store access) ───────────


def test_relay_inbox_invalidates_at_startup_ack_clear_and_terminal() -> None:
    inbox = Inbox(5)
    invalidations: list[int] = []
    waits_seen = iter([0, 0, 0])

    def waited(n: int) -> None:
        _ = next(waits_seen, None)
        if n == 1:
            inbox.pending.remove(5)  # ACK clears the outstanding page
        elif n == 2:
            inbox.status = "released"

    async def invalidate() -> None:
        invalidations.append(len(invalidations))

    hints: list[str] = []
    asyncio.run(
        relay.relay_inbox(
            42,
            LEASE_ID,
            read_inbox=inbox.read,
            listener=Listener(inbox, waited=waited),
            emit=hints.append,
            debounce=0,
            invalidate=invalidate,
        )
    )
    # 1 = startup sweep, 2 = ACK-cleared transition, 3 = terminal sweep
    assert len(invalidations) == 3
    assert "newest_id=5" in hints[0]


def test_relay_inbox_does_not_invalidate_while_outstanding_is_unprocessed() -> None:
    inbox = Inbox(5)
    invalidations: list[int] = []

    def waited(n: int) -> None:
        if n >= 4:
            inbox.active = False

    async def invalidate() -> None:
        invalidations.append(len(invalidations))

    hints: list[str] = []
    asyncio.run(
        relay.relay_inbox(
            42,
            LEASE_ID,
            read_inbox=inbox.read,
            listener=Listener(inbox, waited=waited),
            emit=hints.append,
            debounce=0,
            invalidate=invalidate,
        )
    )
    assert len(hints) == 1
    # Startup sweep + the terminal sweep on release; nothing in between.
    assert len(invalidations) == 2


def test_routine_page_coalesces_into_one_hint_per_window() -> None:
    inbox = Inbox()
    inbox.batch_window = 0.1

    def waited(n: int) -> None:
        if n == 1:
            inbox.pending.add(1)
            inbox.routine.add(1)
        elif n == 2:
            inbox.pending.add(2)
            inbox.routine.add(2)

    listener = SleepingListener(inbox, waited=waited)
    hints: list[str] = []

    def emit(hint: str) -> None:
        hints.append(hint)
        if "inbox ready" in hint:
            inbox.active = False  # stop once the merged hint is delivered

    asyncio.run(
        relay.relay_inbox(
            42, LEASE_ID, read_inbox=inbox.read, listener=listener, emit=emit, debounce=0
        )
    )
    assert len(hints) == 2  # activation, then ONE merged inbox hint for both ids
    assert "control active" in hints[0]
    assert "newest_id=2" in hints[1]
    assert listener.waits[0] == 30.0  # initial catchup
    # The window wait is bounded by the merge window (the 0.5s anti-spin floor
    # may lift a shorter window), never by a full catchup.
    assert 0 < listener.waits[1] < 30.0
    assert listener.closed


def test_user_chat_bypasses_the_merge_window() -> None:
    inbox = Inbox()
    inbox.batch_window = 0.5

    def waited(n: int) -> None:
        if n == 1:
            inbox.pending.add(1)
            inbox.routine.add(1)
        elif n == 2:
            inbox.pending.add(2)  # a user chat: never routine

    listener = SleepingListener(inbox, waited=waited)
    hints: list[str] = []

    def emit(hint: str) -> None:
        hints.append(hint)
        if "inbox ready" in hint:
            inbox.active = False

    asyncio.run(
        relay.relay_inbox(
            42, LEASE_ID, read_inbox=inbox.read, listener=listener, emit=emit, debounce=0
        )
    )
    assert len(hints) == 2
    assert "newest_id=2" in hints[1]
    # The urgent arrival cut the window short: exactly one window-bounded wait.
    assert len([w for w in listener.waits if 0 < w <= 0.5]) == 1


def test_routine_page_hints_immediately_when_window_disabled() -> None:
    inbox = Inbox()
    inbox.batch_window = 0.0

    def waited(n: int) -> None:
        if n == 1:
            inbox.pending.add(1)
            inbox.routine.add(1)

    listener = SleepingListener(inbox, waited=waited)
    hints: list[str] = []

    def emit(hint: str) -> None:
        hints.append(hint)
        if "inbox ready" in hint:
            inbox.active = False

    asyncio.run(
        relay.relay_inbox(
            42, LEASE_ID, read_inbox=inbox.read, listener=listener, emit=emit, debounce=0
        )
    )
    assert len(hints) == 2
    assert "newest_id=1" in hints[1]
    assert listener.waits == [30.0, 30.0]  # no window wait at all


def test_routine_ids_are_only_non_user_non_cancel_arrivals() -> None:
    rows = [
        {"id": 1, "kind": "chat", "source": "user"},
        {"id": 2, "kind": "chat", "source": "agent:99"},
        {"id": 3, "kind": "system_note", "source": "system:impersonation"},
        {"id": 4, "kind": "cancel", "source": "system"},
        {"id": 5, "kind": "chat", "source": "watcher:7"},
    ]
    assert relay._routine_ids(rows) == frozenset({2, 3, 5})


def test_invalidate_our_hints_deletes_only_scoped_hints_real_input_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real Codex 0.153.4 shape: submission.input is a list of content blocks.

    Only this relay's own agent+lease-scoped hints are deleted; real user
    messages and other relays' hints (other agent or other lease) survive.
    """
    executable = tmp_path / "codex"
    log = tmp_path / "rpc.log"
    script = (
        f"#!{sys.executable}\nLOG_PATH = {str(log)!r}\nLEASE = {str(LEASE_ID)!r}\n"
        + """
import json, sys
for line in sys.stdin:
    req = json.loads(line)
    method = req.get("method", "")
    if method == "initialize":
        print(json.dumps({"id": req["id"], "result": {}}), flush=True)
    elif method == "thread/queue/list":
        page = [
            {"id": "hint-1", "clientUserMessageId": "c1",
             "input": [{"type": "text",
                        "text": "Ava inbox ready: agent=42 lease=" + LEASE + " pending_page=1"}]},
            {"id": "user-1", "clientUserMessageId": "c2",
             "input": [{"type": "text", "text": "please refactor the parser"}]},
            {"id": "hint-2", "clientUserMessageId": "c3",
             "input": [{"type": "text",
                        "text": "Ava control active: agent=42 lease=" + LEASE + " pending_page=0"}]},
            {"id": "other-agent", "clientUserMessageId": "c4",
             "input": [{"type": "text",
                        "text": "Ava inbox ready: agent=43 lease=" + LEASE + " pending_page=9"}]},
            {"id": "other-lease", "clientUserMessageId": "c5",
             "input": [{"type": "text",
                        "text": "Ava inbox ready: agent=42 lease=00000000-0000-0000-0000-000000000000 "
                                "pending_page=9"}]},
            {"id": "hint-3", "clientUserMessageId": "c6",
             "input": [{"type": "text",
                        "text": "Ava control expired: agent=42 lease=" + LEASE + "."}]},
        ]
        print(json.dumps({"id": req["id"], "result": {"data": page, "nextCursor": None}}), flush=True)
    elif method == "thread/queue/delete":
        with open(LOG_PATH, "a") as handle:
            handle.write(req["params"]["queuedSubmissionId"] + "\\n")
        print(json.dumps({"id": req["id"], "result": {}}), flush=True)
    elif not method:
        continue  # initialized notification
"""
    )
    executable.write_text(script)
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    assert relay.invalidate_our_hints(THREAD_ID, 42, LEASE_ID) == 3
    assert log.read_text().splitlines() == ["hint-1", "hint-2", "hint-3"]


def test_invalidate_our_hints_accepts_legacy_dict_input_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defensive compatibility: the older dict-with-items input shape still works."""
    executable = tmp_path / "codex"
    log = tmp_path / "rpc.log"
    script = (
        f"#!{sys.executable}\nLOG_PATH = {str(log)!r}\nLEASE = {str(LEASE_ID)!r}\n"
        + """
import json, sys
for line in sys.stdin:
    req = json.loads(line)
    method = req.get("method", "")
    if method == "initialize":
        print(json.dumps({"id": req["id"], "result": {}}), flush=True)
    elif method == "thread/queue/list":
        page = [
            {"id": "hint-1", "clientUserMessageId": "c1",
             "input": {"items": [{"type": "text",
                                  "text": "Ava inbox ready: agent=42 lease=" + LEASE + " pending_page=1"}]}},
            {"id": "user-1", "clientUserMessageId": "c2",
             "input": {"items": [{"type": "text", "text": "please refactor the parser"}]}},
        ]
        print(json.dumps({"id": req["id"], "result": {"data": page, "nextCursor": None}}), flush=True)
    elif method == "thread/queue/delete":
        with open(LOG_PATH, "a") as handle:
            handle.write(req["params"]["queuedSubmissionId"] + "\\n")
        print(json.dumps({"id": req["id"], "result": {}}), flush=True)
    elif not method:
        continue  # initialized notification
"""
    )
    executable.write_text(script)
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    assert relay.invalidate_our_hints(THREAD_ID, 42, LEASE_ID) == 1
    assert log.read_text().splitlines() == ["hint-1"]


def test_hint_prefixes_are_scoped_to_agent_and_lease() -> None:
    prefixes = relay._hint_prefixes(42, LEASE_ID)
    scope = f"agent=42 lease={LEASE_ID}"
    assert all(scope in prefix for prefix in prefixes)
    assert len(prefixes) == 4
    # The relay's own emitted hints all match one scoped prefix.
    for text in (
        relay.inbox_hint(42, LEASE_ID, frozenset({1})),
        relay.activation_hint(42, LEASE_ID, frozenset()),
        "Ava control rejected: agent=42 lease=" + str(LEASE_ID) + ".",
        "Ava control expired: agent=42 lease=" + str(LEASE_ID) + ".",
    ):
        assert any(text.startswith(prefix) for prefix in prefixes), text
    # Neighbour scopes and unrelated text never match.
    for text in (
        "Ava inbox ready: agent=43 lease=" + str(LEASE_ID),
        "Ava inbox ready: agent=42 lease=00000000-0000-0000-0000-000000000000",
        "please refactor the parser",
        "Ava inbox ready: agent=42",
    ):
        assert not any(text.startswith(prefix) for prefix in prefixes), text


def test_invalidate_our_hints_logs_loudly_when_the_rpc_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.log import logger as real_logger

    executable = tmp_path / "codex"
    executable.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(3)\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    warnings: list[str] = []

    class FakeLogger:
        def warning(self, message: str, **fields: object) -> None:
            warnings.append(message)

    monkeypatch.setattr("shared.log.logger", FakeLogger())
    assert relay.invalidate_our_hints(THREAD_ID, 42, LEASE_ID) == 0
    assert len(warnings) == 1
    assert "invalidation unavailable" in warnings[0]
    _ = real_logger
