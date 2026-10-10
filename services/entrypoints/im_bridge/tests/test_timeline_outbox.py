"""Real transactions protect acceptance, replay receipts and stream ownership."""

import asyncio
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any
from uuid import uuid4

import httpx
import pytest
from psycopg import Connection
from psycopg_pool import ConnectionPool

from base.config import settings
from base.db.transaction import write_transaction
from base.deploy.maintenance import admission
from services.entrypoints.im_bridge.outbound.store import IMOutboxStore
from services.entrypoints.im_bridge.outbound.types import (
    OutboundAccountMismatchError,
    OutboundAdapterKind,
    OutboundChunk,
    OutboundIdentityConflictError,
    OutboundIntent,
    OutboundSource,
    OutboundSourceKind,
    OutboundStatus,
    PreparedOutboundSend,
    TimelineCandidate,
)
from services.entrypoints.im_bridge.outbound.worker import IMOutboxWorker
from services.entrypoints.im_bridge.types import IMAdapter, SendNotStartedError


@pytest.fixture
def pool() -> Iterator[ConnectionPool]:
    with ConnectionPool[Connection](settings.data_plane.db_url, min_size=1, max_size=2) as pool:
        yield pool


class RecordingAdapter(IMAdapter):
    channel = "telegram"

    def __init__(self, account: str = "bot") -> None:
        super().__init__(None)
        self.account = account
        self.sent: list[str] = []
        self.error: Exception | None = None
        self.started: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def start(self, tasks: asyncio.TaskGroup) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def outbound_account_id(self) -> str:
        return self.account

    async def prepare_timeline(self, text: str) -> PreparedOutboundSend:
        return PreparedOutboundSend(
            adapter_kind=OutboundAdapterKind.TELEGRAM,
            account_id=self.account,
            chunks=(OutboundChunk(text=text),),
            markdown=False,
        )

    async def send(
        self, chat_id: str, text: str, *, buttons: Any = None, markdown: Any = False
    ) -> None:
        raise AssertionError("timeline delivery must not use command retry owner")

    async def send_prepared_outbound(self, chat_id: str, prepared: PreparedOutboundSend) -> None:
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        self.sent.extend(chunk.text for chunk in prepared.chunks)
        if self.error is not None:
            raise self.error


def candidate(
    index: int = 1,
    *,
    text: str = "original",
    account: str = "bot",
    chat: str = "chat",
    agent: int = 7,
    replay: str = "",
    qualified: bool = True,
) -> TimelineCandidate:
    item = {
        "item_id": f"{index}.0",
        "kind": "agent_chat",
        "payload": text,
        "source_message_id": f"persisted-{index}",
        "source_block_idx": 0,
    }
    intent = OutboundIntent(
        channel="telegram",
        chat_id=chat,
        agent_id=agent,
        source=OutboundSource(
            kind=OutboundSourceKind.MESSAGE, identity=f"persisted-{index}", block_idx=0
        ),
        prepared=PreparedOutboundSend(
            adapter_kind=OutboundAdapterKind.TELEGRAM,
            account_id=account,
            chunks=(OutboundChunk(text=text),),
            markdown=False,
        ),
        replay_id=replay,
    )
    return TimelineCandidate(item, intent if qualified else None)


def statuses(pool: ConnectionPool) -> list[tuple[Any, ...]]:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT status,attempt_id,outcome_reason FROM im_bridge_outbound_intents ORDER BY id"
        ).fetchall()


def test_acceptance_replays_atomic_batch_and_target_conflicts(pool: ConnectionPool) -> None:
    store = IMOutboxStore(pool)
    original = store.accept(
        "telegram", "bot", "chat", 7, [candidate(replay="event")], replay_id="event"
    )
    recovered = store.accept(
        "telegram",
        "bot",
        "chat",
        7,
        [candidate(2, replay="event"), candidate(replay="event")],
        replay_id="event",
    )
    assert recovered == original
    assert len(statuses(pool)) == 1
    with pytest.raises(OutboundIdentityConflictError, match="another target"):
        store.accept(
            "telegram", "bot", "chat", 8, [candidate(agent=8, replay="event")], replay_id="event"
        )


def test_concurrent_acceptance_commits_one_intent_and_cursor(pool: ConnectionPool) -> None:
    def accept(_index: int):
        return IMOutboxStore(pool).accept("telegram", "bot", "chat", 7, [candidate()])

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(accept, range(2)))
    assert sum(len(result.intent_ids) for result in results) == 1
    with pool.connection() as conn:
        assert conn.execute("SELECT push_item_id FROM im_bridge_cursors").fetchone() == ("1.0",)
    assert len(statuses(pool)) == 1


def test_failed_insert_rolls_back_cursor_and_earlier_intent(
    pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = IMOutboxStore(pool)
    original = store.insert_intent

    def fail_second(conn: Connection, intent: OutboundIntent):
        if intent.source.identity == "persisted-2":
            raise RuntimeError("injected insert failure")
        return original(conn, intent)

    monkeypatch.setattr(store, "insert_intent", fail_second)
    with pytest.raises(RuntimeError, match="injected"):
        store.accept("telegram", "bot", "chat", 7, [candidate(), candidate(2)])
    assert statuses(pool) == []
    with pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM im_bridge_cursors").fetchone() == (0,)


def test_changed_manifest_does_not_replace_original(pool: ConnectionPool) -> None:
    store = IMOutboxStore(pool)
    store.accept("telegram", "bot", "chat", 7, [candidate(replay="event")], replay_id="event")
    # A different cursor/renumbered coordinate cannot overwrite the same source request.
    with write_transaction(pool) as conn:
        conn.execute("UPDATE im_bridge_cursors SET push_item_id='0.0'")
    store.accept("telegram", "bot", "chat", 7, [candidate()])
    with write_transaction(pool) as conn:
        conn.execute("UPDATE im_bridge_cursors SET push_item_id='0.0'")
    with pytest.raises(OutboundIdentityConflictError):
        store.accept("telegram", "bot", "chat", 7, [candidate(text="changed")])


def test_legacy_unqualified_blocks_cursor_and_account_rebind_requires_replay(
    pool: ConnectionPool,
) -> None:
    store = IMOutboxStore(pool)
    result = store.accept(
        "telegram", "bot", "chat", 7, [candidate(), candidate(2, qualified=False), candidate(3)]
    )
    assert result.blocked and result.watermark is not None
    assert result.watermark.item_id == "1.0"
    with pytest.raises(OutboundAccountMismatchError):
        store.accept("telegram", "new", "chat", 7, [candidate(2, account="new")])
    store.accept(
        "telegram",
        "new",
        "chat",
        7,
        [candidate(2, account="new", replay="switch")],
        replay_id="switch",
    )
    assert store.pending_streams({"telegram": "new"}) == [("telegram", "new", "chat")]
    assert store.pending_streams({"telegram": "bot"}) == [("telegram", "bot", "chat")]
    store.mark_unavailable_accounts({"telegram": "new"})
    assert statuses(pool)[0][2] == "authenticated_account_unavailable"


def test_legacy_empty_cursor_remains_held_after_account_binding_restart(
    pool: ConnectionPool,
) -> None:
    with write_transaction(pool) as conn:
        conn.execute("INSERT INTO im_bridge_cursors(channel,chat_id) VALUES ('telegram','chat')")
    for _ in range(2):
        result = IMOutboxStore(pool).accept("telegram", "bot", "chat", 7, [candidate()])
        assert result.blocked and not result.intent_ids
    assert statuses(pool) == []
    store = IMOutboxStore(pool)
    store.accept("telegram", "bot", "chat", 7, [], replay_id="empty-switch")
    assert store.accept("telegram", "bot", "chat", 7, [candidate()]).intent_ids


def test_old_accounts_cannot_starve_current_account_limit(pool: ConnectionPool) -> None:
    store = IMOutboxStore(pool)
    for index in range(20):
        store.accept(
            "telegram", "old", f"old-{index}", 7, [candidate(account="old", chat=f"old-{index}")]
        )
    store.accept("telegram", "bot", "chat", 7, [candidate()])
    assert store.pending_streams({"telegram": "bot"}) == [("telegram", "bot", "chat")]


@pytest.mark.parametrize(
    "error,expected",
    [
        (None, "sent"),
        (SendNotStartedError("no effect"), "failed"),
        (httpx.ReadError("https://provider/botSECRET?context_token=SECRET"), "uncertain"),
    ],
)
async def test_worker_records_whole_send_outcome_without_secret(
    pool: ConnectionPool, error: Any, expected: str
) -> None:
    store = IMOutboxStore(pool)
    store.accept("telegram", "bot", "chat", 7, [candidate()])
    adapter = RecordingAdapter()
    adapter.error = error
    await IMOutboxWorker(store, {"telegram": adapter}).run_once()
    row = statuses(pool)[0]
    assert row[0] == expected and row[1] is not None
    assert "SECRET" not in str(row)
    await IMOutboxWorker(store, {"telegram": adapter}).run_once()
    assert adapter.sent == ["original"]


async def test_blocked_send_releases_single_pool_lease_and_other_worker_claims_later(
    pool: ConnectionPool,
) -> None:
    with ConnectionPool[Connection](settings.data_plane.db_url, min_size=1, max_size=1) as small:
        store = IMOutboxStore(small)
        store.accept("telegram", "bot", "chat", 7, [candidate(), candidate(2, text="later")])
        live = RecordingAdapter()
        live.started, live.release = asyncio.Event(), asyncio.Event()
        worker = asyncio.create_task(IMOutboxWorker(store, {"telegram": live}).run_once())
        try:
            await asyncio.wait_for(live.started.wait(), timeout=5)
            # A real max_size=1 lease is available while the provider is blocked.
            with small.connection(timeout=0.5) as conn:
                assert conn.execute("SELECT 1").fetchone() == (1,)
            original_attempt = statuses(pool)[0][1]
            restarted = RecordingAdapter()
            await asyncio.wait_for(
                IMOutboxWorker(IMOutboxStore(pool), {"telegram": restarted}).run_once(), timeout=5
            )
            assert restarted.sent == ["later"]
            assert statuses(pool)[0] == ("sending", original_attempt, "send_result_unconfirmed")
            worker.cancel()
            await asyncio.sleep(0.02)
            assert not worker.done(), "cancellation still collects the actual SDK send"
        finally:
            live.release.set()
            with pytest.raises(asyncio.CancelledError):
                await worker
        assert live.sent == ["original"]
        await IMOutboxWorker(IMOutboxStore(pool), {"telegram": restarted}).run_once()
        assert [row[0] for row in statuses(pool)] == ["sending", "sent"]
        assert restarted.sent == ["later"]


def test_two_concurrent_claims_cannot_start_the_same_intent(pool: ConnectionPool) -> None:
    store = IMOutboxStore(pool)
    [intent_id] = store.accept("telegram", "bot", "chat", 7, [candidate()]).intent_ids

    def claim(_index: int):
        return store.claim(("telegram", "bot", "chat"))

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim, range(2)))
    claimed = [claim for claim in claims if claim is not None]
    assert len(claimed) == 1 and claimed[0][0] == intent_id
    assert statuses(pool)[0] == ("sending", claimed[0][1], "send_result_unconfirmed")
    assert not store.finish(intent_id, uuid4(), OutboundStatus.SENT, None)
    assert store.finish(intent_id, claimed[0][1], OutboundStatus.SENT, None)


async def test_claim_without_send_is_unconfirmed_and_does_not_block_later(pool: ConnectionPool):
    store = IMOutboxStore(pool)
    store.accept("telegram", "bot", "chat", 7, [candidate(), candidate(2, text="later")])
    claimed = store.claim(("telegram", "bot", "chat"))
    assert claimed is not None
    adapter = RecordingAdapter()
    await IMOutboxWorker(store, {"telegram": adapter}).run_once()
    await IMOutboxWorker(store, {"telegram": adapter}).run_once()
    assert adapter.sent == ["later"]
    assert [row[0] for row in statuses(pool)] == ["sending", "sent"]
    # A still-live original owner may finish; no recovery rewrites its authority.
    assert store.finish(claimed[0], claimed[1], OutboundStatus.SENT, None)


@pytest.mark.parametrize("committed", [False, True])
async def test_success_before_outcome_commit_failure_never_repeats_external_send(
    pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    store = IMOutboxStore(pool)
    original = store.accept("telegram", "bot", "chat", 7, [candidate()]).intent_ids[0]
    adapter = RecordingAdapter()
    finish = store.finish

    def lost(*args: Any):
        if committed:
            finish(*args)
        raise RuntimeError("outcome commit acknowledgement lost")

    monkeypatch.setattr(store, "finish", lost)
    with pytest.raises(RuntimeError, match="acknowledgement"):
        await IMOutboxWorker(store, {"telegram": adapter}).run_once()
    old_attempt = statuses(pool)[0][1]
    monkeypatch.setattr(store, "finish", finish)
    await IMOutboxWorker(store, {"telegram": adapter}).run_once()
    assert statuses(pool)[0][0] == ("sent" if committed else "sending")
    assert adapter.sent == ["original"]
    assert not finish(original, uuid4(), OutboundStatus.SENT, None)
    assert statuses(pool)[0][1] == old_attempt
    store.accept("telegram", "bot", "chat", 7, [candidate(2, text="later")])
    await IMOutboxWorker(store, {"telegram": adapter}).run_once()
    assert adapter.sent == ["original", "later"]


async def test_quiesce_between_streams_finishes_active_attempt_and_holds_new_claim(
    pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = False
    monkeypatch.setattr(admission, "quiesced", lambda: held)
    store = IMOutboxStore(pool)
    store.accept("telegram", "bot", "first", 7, [candidate(chat="first", text="first")])
    store.accept("telegram", "bot", "second", 7, [candidate(chat="second", text="second")])
    adapter = RecordingAdapter()
    adapter.started, adapter.release = asyncio.Event(), asyncio.Event()
    worker = IMOutboxWorker(store, {"telegram": adapter})
    running = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(adapter.started.wait(), timeout=5)
    held = True
    adapter.release.set()
    await asyncio.wait_for(running, timeout=5)
    assert adapter.sent == ["first"]
    assert [row[0] for row in statuses(pool)] == ["sent", "queued"]
    await worker.run_once()
    assert adapter.sent == ["first"]
    held = False
    await worker.run_once()
    assert adapter.sent == ["first", "second"]
    assert [row[0] for row in statuses(pool)] == ["sent", "sent"]


@pytest.mark.parametrize("finish_fails", [False, True])
async def test_programming_error_is_visible_then_rethrown_without_resend(
    pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch, finish_fails: bool
) -> None:
    store = IMOutboxStore(pool)
    store.accept("telegram", "bot", "chat", 7, [candidate()])
    adapter = RecordingAdapter()
    original = TypeError("invalid adapter state SECRET")
    adapter.error = original
    finish_error = RuntimeError("outcome storage failed")
    finish = store.finish
    if finish_fails:

        def failed_finish(*args: Any):
            raise finish_error

        monkeypatch.setattr(store, "finish", failed_finish)
    with pytest.raises(TypeError) as caught:
        await IMOutboxWorker(store, {"telegram": adapter}).run_once()
    assert caught.value is original
    assert caught.value.__cause__ is (finish_error if finish_fails else None)
    assert statuses(pool)[0][0] == ("sending" if finish_fails else "uncertain")
    assert "SECRET" not in str(statuses(pool))
    monkeypatch.setattr(store, "finish", finish)
    adapter.error = None
    store.accept("telegram", "bot", "chat", 7, [candidate(2, text="later")])
    await IMOutboxWorker(store, {"telegram": adapter}).run_once()
    assert adapter.sent == ["original", "later"]


async def test_partial_prefix_is_never_replayed_as_a_whole_manifest(pool: ConnectionPool):
    from services.entrypoints.im_bridge.types import SendOutcomeUncertainError

    class PartialAdapter(RecordingAdapter):
        async def send_prepared_outbound(
            self, chat_id: str, prepared: PreparedOutboundSend
        ) -> None:
            self.sent.append(prepared.chunks[0].text)
            raise SendOutcomeUncertainError("prefix delivered; suffix unknown")

    store = IMOutboxStore(pool)
    first = candidate()
    assert first.intent is not None
    # Preserve the real immutable, multi-part request through acceptance.
    first = TimelineCandidate(
        first.item,
        first.intent.model_copy(
            update={
                "prepared": first.intent.prepared.model_copy(
                    update={"chunks": (OutboundChunk(text="prefix"), OutboundChunk(text="suffix"))}
                )
            }
        ),
    )
    store.accept("telegram", "bot", "chat", 7, [first])
    partial = PartialAdapter()
    await IMOutboxWorker(store, {"telegram": partial}).run_once()
    await IMOutboxWorker(store, {"telegram": partial}).run_once()
    assert partial.sent == ["prefix"]
    assert statuses(pool)[0][0] == "uncertain"
    store.accept("telegram", "bot", "chat", 7, [candidate(2, text="later")])
    later = RecordingAdapter()
    await IMOutboxWorker(store, {"telegram": later}).run_once()
    assert later.sent == ["later"]
    assert [row[0] for row in statuses(pool)] == ["uncertain", "sent"]


async def test_cancelled_send_still_exposes_collected_programming_error(pool: ConnectionPool):
    store = IMOutboxStore(pool)
    store.accept("telegram", "bot", "chat", 7, [candidate()])
    started, release = asyncio.Event(), Event()
    original = TypeError("late SDK result invalid")
    loop = asyncio.get_running_loop()

    class SDKAdapter(RecordingAdapter):
        async def send_prepared_outbound(
            self, chat_id: str, prepared: PreparedOutboundSend
        ) -> None:
            def blocking_sdk() -> None:
                loop.call_soon_threadsafe(started.set)
                assert release.wait(timeout=5), "test must release the real SDK thread"
                self.sent.append(prepared.chunks[0].text)
                raise original

            await asyncio.to_thread(blocking_sdk)

    adapter = SDKAdapter()
    task = asyncio.create_task(IMOutboxWorker(store, {"telegram": adapter}).run_once())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(TypeError) as caught:
        await asyncio.wait_for(task, timeout=5)
    assert caught.value is original
    assert isinstance(original.__cause__, asyncio.CancelledError)
    assert statuses(pool)[0][0] == "uncertain"
    await IMOutboxWorker(store, {"telegram": adapter}).run_once()
    assert adapter.sent == ["original"]
