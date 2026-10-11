"""Upload delivery rounds: copy each accepted batch, accept one chat, wake once.

A batch gets at most `source.MAX_COPY_ATTEMPTS` copy attempts; the round then holds it
and emits `upload_delivery_held`. Rounds drain before the Gateway closes its pool.
"""

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from functools import partial
from typing import Any

from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from base import telemetry
from base.agents import AgentNotFound
from base.agents.messages.inbound import InboundKind
from base.agents.upload_delivery import source, storage
from base.agents.upload_delivery.models import (
    NAMESPACE,
    CopyProof,
    ReceiveRequest,
    UploadDeliveryConflictError,
)
from base.agents.upload_delivery.paths import agent_upload_dir
from base.db import Database, publish_inbound_wake
from base.deploy.maintenance.admission import business_paused, quiesced
from base.events.live.bus import EventBus
from gateway.http.middleware.stopping import is_stopping
from ops.cluster.rpc import ClusterOpFailed, ClusterOpTargetAbsent, dispatch_to_machine
from ops.lifecycle import resurrect_if_terminated

_log = logging.getLogger(__name__)


def _cluster_failure(exc: ClusterOpFailed) -> tuple[str, bool]:
    """The safe reason of a failed receiver call and whether it can never succeed."""
    if str(exc.result.get("error", "")).startswith("unknown kind:"):
        return "unsupported-receiver", True
    reason = exc.result.get("reason")
    if reason in {
        "upload-copy-conflict-v1",
        "upload-copy-quota-v1",
        "upload-source-unavailable-v1",
    }:
        return str(reason), True
    return "receiver-unavailable", False


class UploadRecovery:
    """A source domain round and its actual native futures; never time-based leases."""

    def __init__(self, pool: ConnectionPool, db: Database, bus: EventBus) -> None:
        self.pool, self.db, self.bus = pool, db, bus
        self.stopped = asyncio.Event()
        self.calls: set[asyncio.Future[Any]] = set()
        self.task: asyncio.Task[None] | None = None

    async def native(self, call: Callable[..., Any], *args: Any) -> Any:
        task = asyncio.get_running_loop().run_in_executor(None, partial(call, *args))
        self.calls.add(task)
        task.add_done_callback(self.finished_call)
        # Cancellation does not stop a DB/filesystem thread. Retain it until the
        # actual call finishes, and lifespan.close drains it before pool closure.
        return await asyncio.shield(task)

    def finished_call(self, future: asyncio.Future[Any]) -> None:
        self.calls.discard(future)
        if not future.cancelled():
            future.exception()  # Consume errors even if the HTTP await was cancelled.

    def start(self, group: asyncio.TaskGroup) -> None:
        self.task = group.create_task(self.run())

    async def close(self) -> None:
        self.stopped.set()
        cancelled = False
        if self.task is not None:
            while not self.task.done():
                try:
                    await asyncio.shield(self.task)
                except asyncio.CancelledError:
                    cancelled = True
        while self.calls:
            try:
                await asyncio.shield(asyncio.gather(*self.calls, return_exceptions=True))
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def run(self) -> None:
        while not self.stopped.is_set():
            try:
                if (
                    not is_stopping()
                    and not await self.native(business_paused)
                    and not await self.native(quiesced)
                ):
                    await self.round()
            except Exception:
                _log.exception(
                    "delivered upload recovery round failed; retained intents remain recoverable"
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(self.stopped.wait(), timeout=5.0)

    async def round(self) -> None:
        requests = await self.native(source.due, self.pool, 4)
        async with asyncio.TaskGroup() as group:
            for request in requests:
                group.create_task(self.copy(request))

    async def wake(self, agent_id: int, inbound_id: int) -> None:
        """Wake the target once after acceptance; a failure is logged and not retried.

        The accepted chat is already a pending inbound, so the agent's next ordinary wake
        (a message or heartbeat) delivers it.
        """
        try:
            await self.native(publish_inbound_wake, self.db, self.bus, agent_id, str(inbound_id))
            await resurrect_if_terminated(
                self.db,
                self.bus,
                agent_id,
                trigger_inbound_id=inbound_id,
                trigger_inbound_kind=InboundKind.CHAT,
            )
        except Exception:
            _log.warning(
                "upload inbound wake failed for agent=%s iid=%s; not retried",
                agent_id,
                inbound_id,
                exc_info=True,
            )

    async def copy(self, request: ReceiveRequest) -> None:
        if self.stopped.is_set() or await self.native(business_paused):
            return
        try:
            if not await self.native(source.validate_pending, self.pool, request):
                return
            proof = await self.copy_proof(request)
            # Paused units finish their owned copy, but do not admit a fresh chat
            # while stop is certified. Retained pending intent retries after resume.
            if (
                self.stopped.is_set()
                or await self.native(business_paused)
                or await self.native(quiesced)
            ):
                return
            inbound_id = await self.native(source.complete, self.pool, request, proof)
        except ClusterOpFailed as exc:
            reason, hold = _cluster_failure(exc)
            await self.failure(request, reason, hold=hold)
        except (
            UploadDeliveryConflictError,
            AgentNotFound,
            ValidationError,
            ClusterOpTargetAbsent,
        ) as exc:
            await self.failure(
                request,
                str(exc) if isinstance(exc, UploadDeliveryConflictError) else type(exc).__name__,
                hold=True,
            )
        except Exception as exc:
            await self.failure(request, type(exc).__name__, hold=False)
        else:
            await self.wake(request.manifest.agent_id, inbound_id)

    async def copy_proof(self, request: ReceiveRequest) -> CopyProof:
        if request.source == request.target:
            directory = agent_upload_dir(request.manifest.agent_id, create=False).resolve()
            await self.native(storage.verify_all, directory, request.manifest)
            return CopyProof(
                version=1,
                target=request.target,
                manifest_hash=request.manifest.fingerprint(),
                directory=str(directory / NAMESPACE / request.manifest.batch_id),
            )
        result = await dispatch_to_machine(
            self.db,
            request.target.machine,
            "upload-receive-v1",
            request.model_dump(mode="json"),
            timeout_s=130.0,
            retries=0,
        )
        return CopyProof.model_validate(result)

    async def failure(self, request: ReceiveRequest, reason: str, *, hold: bool) -> None:
        state = await self.native(
            lambda: source.record_failure(self.pool, request.manifest.batch_id, reason, hold=hold)
        )
        if state == "hold":
            telemetry.emit(
                "telemetry",
                "upload_delivery_held",
                attributes={
                    "agent_id": request.manifest.agent_id,
                    "batch_id": request.manifest.batch_id,
                    "reason": reason,
                },
            )
