import asyncio
from collections.abc import Awaitable, Callable

from base.deploy.maintenance import admission
from base.log import logger
from services.entrypoints.im_bridge.outbound.store import IMOutboxStore
from services.entrypoints.im_bridge.outbound.types import OutboundIntent, OutboundStatus
from services.entrypoints.im_bridge.types import (
    NETWORK_ERRORS,
    IMAdapter,
    SendNotStartedError,
    SendOutcomeUncertainError,
)


async def _complete_before_cancel[T](work: Awaitable[T]) -> T:
    """Collect an already-started external call or short commit before cancellation.

    Shielding also covers adapters awaiting blocking SDK calls in a thread:
    cancelling their coroutine would otherwise hide a still-running send.
    """
    result: list[T] = []
    errors: list[Exception] = []
    cancelled: asyncio.CancelledError | None = None

    async def capture() -> None:
        try:
            result.append(await work)
        except Exception as exc:
            errors.append(exc)

    async with asyncio.TaskGroup() as group:
        task = group.create_task(capture())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            # Drain the actual send/commit, including blocking SDK work.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            cancelled = cancellation
    if errors:
        if cancelled is not None:
            raise errors[0] from cancelled
        raise errors[0]
    if cancelled is not None:
        raise cancelled
    return result[0]


class IMOutboxWorker:
    def __init__(
        self,
        store: IMOutboxStore,
        adapters: dict[str, IMAdapter],
        *,
        retry: Callable[[Callable[[], Awaitable[None]]], Awaitable[None]] | None = None,
    ) -> None:
        self.store = store
        self.adapters = adapters
        self._retry = retry
        self._serial = asyncio.Lock()

    async def run_once(self) -> None:
        """At most one active whole-send per daemon; old accounts remain queued."""
        if admission.quiesced():
            return
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
        claimed = await _complete_before_cancel(asyncio.to_thread(self.store.claim, stream))
        if claimed is None:
            return
        intent_id, attempt, intent = claimed
        adapter = self.adapters[stream[0]]
        status, reason, unexpected = await self._deliver(adapter, intent, intent_id)
        try:
            recorded = await _complete_before_cancel(
                asyncio.to_thread(self.store.finish, intent_id, attempt, status, reason)
            )
        except Exception as finish_error:
            if unexpected is not None:
                raise unexpected from finish_error
            raise
        if not recorded:
            logger.warning("IM outbound outcome CAS rejected intent={}", intent_id)
        if unexpected is not None:
            raise unexpected

    async def _deliver(
        self, adapter: IMAdapter, intent: OutboundIntent, intent_id: int
    ) -> tuple[OutboundStatus, str | None, Exception | None]:
        async def send_once() -> None:
            await _complete_before_cancel(
                adapter.send_prepared_outbound(intent.chat_id, intent.prepared)
            )

        try:
            await send_once()
        except SendNotStartedError:
            if self._retry is None:
                return OutboundStatus.FAILED, "adapter_proved_no_effect", None
            logger.warning("IM outbound send proved no effect intent={}; retrying once", intent_id)
            try:
                await self._retry(send_once)
            except SendNotStartedError:
                logger.warning("IM outbound send proved no effect after retry intent={}", intent_id)
                return OutboundStatus.FAILED, "adapter_proved_no_effect", None
            except Exception as exc:
                logger.warning(
                    "IM outbound send uncertain intent={} class={}", intent_id, type(exc).__name__
                )
                unexpected = (
                    None if isinstance(exc, (*NETWORK_ERRORS, SendOutcomeUncertainError)) else exc
                )
                return OutboundStatus.UNCERTAIN, "external_send_ambiguous", unexpected
            return OutboundStatus.SENT, None, None
        except Exception as exc:
            logger.warning(
                "IM outbound send uncertain intent={} class={}", intent_id, type(exc).__name__
            )
            unexpected = (
                None if isinstance(exc, (*NETWORK_ERRORS, SendOutcomeUncertainError)) else exc
            )
            return OutboundStatus.UNCERTAIN, "external_send_ambiguous", unexpected
        return OutboundStatus.SENT, None, None
