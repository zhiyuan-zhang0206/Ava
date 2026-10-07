"""One whole-send owner, fenced by a PgBouncer-compatible transaction gate."""

import asyncio
import json
from collections.abc import Awaitable

from base.db.transaction import write_transaction
from base.deploy.maintenance import admission
from base.log import logger
from services.entrypoints.im_bridge.outbound_store import IMOutboxStore
from services.entrypoints.im_bridge.outbound_types import OutboundStatus
from services.entrypoints.im_bridge.types import IMAdapter, SendNotStartedError


async def _complete_before_cancel[T](work: Awaitable[T]) -> T:
    """Do not release the gate while an external call or short commit is alive.

    Shielding also covers adapters awaiting blocking SDK calls in a thread:
    cancelling their coroutine would otherwise hide a still-running send.
    """
    result: list[T] = []
    errors: list[Exception] = []

    async def capture() -> None:
        try:
            result.append(await work)
        except Exception as exc:
            errors.append(exc)

    async with asyncio.TaskGroup() as group:
        task = group.create_task(capture())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Drain an already-started send/commit before gate release.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            raise
    if errors:
        raise errors[0]
    return result[0]


class IMOutboxWorker:
    def __init__(self, store: IMOutboxStore, adapters: dict[str, IMAdapter]) -> None:
        self.store = store
        self.adapters = adapters
        self._serial = asyncio.Lock()

    def validate_pool(self) -> None:
        if self.store._pool().max_size < 2:
            raise ValueError("IM outbound worker requires a database pool with max_size >= 2")

    async def run_once(self) -> None:
        """At most one active whole-send per daemon; old accounts remain queued."""
        if admission.quiesced():
            return
        self.validate_pool()
        async with self._serial:
            accounts: dict[str, str] = {}
            for channel, adapter in self.adapters.items():
                try:
                    accounts[channel] = await adapter.outbound_account_id()
                except Exception as exc:
                    logger.warning(
                        "IM outbound account unavailable channel={} class={}",
                        channel,
                        type(exc).__name__,
                    )
            await asyncio.to_thread(self.store.mark_unavailable_accounts, accounts)
            streams = await asyncio.to_thread(self.store.pending_streams, accounts)
            for stream in streams:
                if admission.quiesced():
                    return
                await self._dispatch(stream)

    async def _dispatch(self, stream: tuple[str, str, str]) -> None:
        # A pins its backend for the network call; B commits sending and the
        # outcome independently. No cursor/intent row lock spans the network.
        # Try-lock prevents two daemons each holding A while waiting for B.
        with write_transaction(self.store._pool(), timeout=1.0) as gate:
            locked = gate.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 4477))",
                (json.dumps(["im-timeline-outbox", *stream]),),
            ).fetchone()
            if locked is None or not locked[0]:
                return
            claimed = await _complete_before_cancel(asyncio.to_thread(self.store.claim, stream))
            if claimed is None:
                return
            intent_id, attempt, intent = claimed
            adapter = self.adapters[stream[0]]
            try:
                await _complete_before_cancel(
                    adapter.send_prepared_outbound(intent.chat_id, intent.prepared)
                )
            except SendNotStartedError:
                status, reason = OutboundStatus.FAILED, "adapter_proved_no_effect"
            except Exception as exc:
                status, reason = OutboundStatus.UNCERTAIN, "external_send_ambiguous"
                logger.warning(
                    "IM outbound send uncertain intent={} class={}", intent_id, type(exc).__name__
                )
            else:
                status, reason = OutboundStatus.SENT, None
            recorded = await _complete_before_cancel(
                asyncio.to_thread(self.store.finish, intent_id, attempt, status, reason)
            )
            if not recorded:
                logger.warning("IM outbound outcome CAS rejected intent={}", intent_id)
