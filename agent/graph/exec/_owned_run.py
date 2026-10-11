"""Actual exec consumer for explicitly admitted durable resource sets.

Legacy NULL rows keep the existing protocol-zero path. No environment flag,
request label or installed revision enables managed resource authority.
"""

import asyncio
import hashlib
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import psutil

from base.agents.context import AvaContext
from base.agents.incarnation.exec_owner_protocol import (
    OwnerClosed,
    OwnerContext,
    OwnerControl,
    OwnerReady,
    publish_owner_message,
    read_owner_bytes,
    validate_native_ready,
)
from base.agents.incarnation.resources import (
    ExecAllocation,
    IncarnationResources,
    ResourceEvidenceError,
    ResourceProcess,
    attach_exec,
    complete_exec,
    decode_resources,
    register_exec,
)
from base.db import Database
from base.log import logger
from base.native_process.exec_domain import KILL_GRACE_S
from base.native_process.exec_kill_notice import read_notice
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.paths import exec_run_dir

from ._output_pipe import ExecOutputPipe
from ._result import _ExecCrashed, _ExecResult
from ._stream import ExecOutputChunkPublisher, StreamingTextIO
from .protocol import ResultPayload, write_request


def managed_target(
    db: Database, agent_id: int | None, *, incarnation: RuntimeIncarnation | None
) -> RuntimeIncarnation | None:
    if agent_id is None:
        return None
    target = None if incarnation is None else incarnation.require_agent(agent_id)
    if target is None:
        return None
    with db.write_transaction() as conn:
        row = conn.execute(
            "SELECT incarnation_resources FROM agents_meta WHERE id=%s AND runtime_generation=%s AND runtime_owner=%s",
            (agent_id, target.generation, target.owner),
        ).fetchone()
        if row is None:
            raise ResourceEvidenceError("exec runtime lost admission")
        if row[0] is None:
            return None
        state = decode_resources(row[0])
        if not isinstance(state, IncarnationResources):
            raise ResourceEvidenceError("exec runtime has not admitted its resource set")
        return target


def _register_attached(db: Database, context: OwnerContext, ready: OwnerReady) -> None:
    """Publish only an attached allocation; force can win before this transaction."""
    with db.write_transaction() as conn:
        target = RuntimeIncarnation(context.agent_id, context.generation, context.runtime_owner)
        register_exec(conn, target, context.allocation)
        attach_exec(
            conn,
            target,
            context.allocation,
            ready.allocation,
        )


def validate_closed(context: OwnerContext, attached: ExecAllocation, path: Path) -> OwnerClosed:
    receipt = OwnerClosed.model_validate_json(read_owner_bytes(path))
    if receipt.allocation != attached or attached.owner_process is None:
        raise ResourceEvidenceError("terminal owner receipt differs from exact allocation")
    if receipt.observed_at.tzinfo is None or receipt.observed_at > datetime.now(UTC):
        raise ResourceEvidenceError("terminal owner receipt has an invalid observation time")
    if (
        hashlib.sha256(read_owner_bytes(context.request_path, 64 * 1024 * 1024)).hexdigest()
        != attached.request_digest
    ):
        raise ResourceEvidenceError("terminal owner request has changed")
    return receipt


def _complete(db: Database, context: OwnerContext, attached: ExecAllocation) -> None:
    with db.write_transaction() as conn:
        complete_exec(
            conn,
            RuntimeIncarnation(context.agent_id, context.generation, context.runtime_owner),
            attached,
        )


class _OwnedRun:
    """One managed exec: one object retains the exact allocation and subprocess ownership."""

    def __init__(
        self,
        db: Database,
        target: RuntimeIncarnation,
        code: str,
        context: AvaContext,
        cancel_event: asyncio.Event,
        timeout: float,
        chunk_publisher: ExecOutputChunkPublisher | None,
        *,
        accumulation_max_chars: int,
        state: dict[str, Any] | None,
        exec_dir: Path | None,
    ) -> None:
        self.db = db
        self.cancel_event = cancel_event
        self.chunk_publisher = chunk_publisher
        self.request_id = uuid4()
        directory = (
            (exec_dir or exec_run_dir()) / str(target.agent_id) / "domains" / str(self.request_id)
        ).resolve()
        directory.mkdir(parents=True, mode=0o700)
        self.agent_id = target.agent_id
        self.request = directory / f"req-{self.request_id.hex}.json"
        self.result = directory / "result.json"
        self.context_path = directory / "owner.json"
        deadline = datetime.now(UTC) + timedelta(seconds=timeout)
        write_request(
            self.request,
            code=code,
            context=context.describe(),
            timeout_s=timeout,
            state=state,
            incarnation=context.original_incarnation,
        )
        self.allocation = ExecAllocation(
            request=self.request_id,
            domain=uuid4(),
            request_digest=hashlib.sha256(self.request.read_bytes()).hexdigest(),
            deadline=deadline,
        )
        self.context = OwnerContext(
            agent_id=target.agent_id,
            generation=target.generation,
            runtime_owner=target.owner,
            request_path=self.request,
            result_path=self.result,
            allocation=self.allocation,
        )
        publish_owner_message(self.context_path, self.context)
        self.scope = context.hosted_resources
        if self.scope is not None:
            self.scope.unresolved[self.request] = None
        self.stream = StreamingTextIO(max_chars=accumulation_max_chars)
        self.proc: subprocess.Popen[bytes] | None = None
        self.ready: OwnerReady | None = None
        self.reader: ExecOutputPipe | None = None
        self.cancelled = False
        self.settled = False
        self.attached = False
        self._tasks: set[asyncio.Task[Any]] = set()
        self._errors: list[BaseException] = []
        self.registration: asyncio.Task[None] | None = None
        self.completion: asyncio.Task[OwnerClosed] | None = None
        self.bound = (
            time.monotonic() + max(0, (deadline - datetime.now(UTC)).total_seconds()) + KILL_GRACE_S
        )

    def _register(self, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._completed)

    def _completed(self, task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self._errors.append(error)
                logger.opt(exception=error).error(
                    "managed exec resource task failed: {name}", name=task.get_name()
                )

    def _raise_failure(self) -> None:
        if self._errors:
            raise self._errors[0]

    async def stop(self, timeout: float) -> tuple[asyncio.Task[Any], ...]:
        """EOF requests native closure; unfinished registration and receipt tasks stay owned."""
        self.close_stdin()
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=timeout)
        self._raise_failure()
        return tuple(task for task in self._tasks if not task.done())

    def start_registration(self, ready: OwnerReady) -> asyncio.Task[None]:
        if self.registration is not None:
            raise RuntimeError("managed exec registration has already started")
        self.registration = asyncio.create_task(
            asyncio.to_thread(_register_attached, self.db, self.context, ready),
            name=f"exec-owner-register-{self.request_id}",
        )
        self._register(self.registration)
        return self.registration

    def attached_completion(self) -> asyncio.Task[OwnerClosed]:
        """Keep the same receipt task across cancellation and commit boundaries."""
        if self.completion is None:
            self.completion = asyncio.create_task(
                self.settle_attached_owner(), name=f"exec-owner-complete-{self.request_id}"
            )
            self._register(self.completion)
        return self.completion

    def _send(self, action: Literal["permit", "cancel"]) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None or proc.stdin.closed:
            return
        message = OwnerControl(
            request=self.request_id, domain=self.allocation.domain, action=action
        )
        proc.stdin.write(message.model_dump_json().encode() + b"\n")
        proc.stdin.flush()

    async def settle_unpermitted_owner(self) -> None:
        """Prove a gated owner closed without permission before forgetting its scope."""
        proc, ready, reader = self.proc, self.ready, self.reader
        if proc is None or ready is None or reader is None:
            raise ResourceEvidenceError("unpermitted owner lacks exact ready evidence")
        if proc.stdin is not None and not proc.stdin.closed:
            proc.stdin.close()
        code = await asyncio.to_thread(proc.wait, max(0.001, self.bound - time.monotonic()))
        if code != 0:
            raise ResourceEvidenceError("unpermitted owner did not close successfully")
        receipt = validate_closed(
            self.context, ready.allocation, self.context_path.with_suffix(".closed")
        )
        if receipt.reason != "host_eof":
            raise ResourceEvidenceError("unpermitted owner closed for an unexpected reason")
        await reader.finish(max(0, self.bound - time.monotonic()))
        if not reader.closed:
            raise ResourceEvidenceError("unpermitted owner output remains unresolved")

    async def settle_attached_owner(self) -> OwnerClosed:
        """Consume one exact owner receipt before releasing durable allocation."""
        proc, ready, reader = self.proc, self.ready, self.reader
        if proc is None or ready is None or reader is None or not self.attached:
            raise ResourceEvidenceError("attached owner lacks exact local completion evidence")
        code = await asyncio.to_thread(proc.wait, max(0.001, self.bound - time.monotonic()))
        if code != 0:
            raise ResourceEvidenceError("attached owner did not close successfully")
        receipt = await asyncio.to_thread(
            validate_closed,
            self.context,
            ready.allocation,
            self.context_path.with_suffix(".closed"),
        )
        await reader.finish(max(0, self.bound - time.monotonic()))
        if not reader.closed:
            raise ResourceEvidenceError("owner output reader remains unresolved")
        await asyncio.to_thread(_complete, self.db, self.context, ready.allocation)
        if self.scope is not None:
            self.scope.complete(self.request, ready)
        return receipt

    async def finish_despite_cancellation(self, task: asyncio.Task[Any]) -> Any:
        """Wait for one retained owner task even if cancellation repeats."""
        while not task.done() and time.monotonic() < self.bound:
            try:
                await asyncio.wait({task}, timeout=max(0, self.bound - time.monotonic()))
            except asyncio.CancelledError:
                continue
        if not task.done():
            raise TimeoutError(f"managed exec task remains unfinished: {task.get_name()}")
        return task.result()

    def launch(
        self, config_overlay: dict[str, object] | None, birth_config: dict[str, object] | None
    ) -> tuple[subprocess.Popen[bytes], ResourceProcess]:
        """Spawn the isolated owner; returns it with its launcher identity."""
        from ._subprocess import _build_child_env

        if self.proc is not None:
            raise RuntimeError("managed exec owner has already launched")
        env = _build_child_env(
            self.agent_id,
            self.request,
            self.result,
            config_overlay=config_overlay,
            birth_config=birth_config,
        )
        proc = subprocess.Popen(  # noqa: S603 -- fixed isolated owner entry, inherited prepared runtime.
            [
                sys.executable,
                "-I",
                "-B",
                "-X",
                "utf8",
                "-m",
                "agent.execution.domain_owner",
                "--context",
                str(self.context_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            close_fds=True,
        )
        self.proc = proc
        return proc, ResourceProcess.capture(psutil.Process(proc.pid))

    def ready_pending(self) -> bool:
        return self.ready is None and self.context_path.with_suffix(".ready").exists()

    def read_ready(self, launcher: ResourceProcess) -> OwnerReady:
        """Validate the owner's ready evidence before its allocation is registered."""
        ready = OwnerReady.model_validate_json(
            read_owner_bytes(self.context_path.with_suffix(".ready"))
        )
        self.ready = ready
        validate_native_ready(ready, launcher, self.context_path)
        return ready

    async def finish_attach(self, registration: asyncio.Task[None], ready: OwnerReady) -> None:
        """Await the registration, then record the attachment and permit the exec."""
        await asyncio.shield(registration)
        self.attached = True
        if self.scope is not None:
            self.scope.unresolved[self.request] = ready
        self._send("permit")

    async def tick(self) -> None:
        """One poll beat: forward cancel, enforce the original bound, stream output."""
        if self.reader is not None:
            self.reader.pump()
        if self.cancel_event.is_set() and not self.cancelled:
            self.cancelled = True
            self._send("cancel")
        if time.monotonic() >= self.bound:
            raise ResourceEvidenceError("owner did not settle within the original exec bound")
        if self.chunk_publisher is not None:
            self.chunk_publisher.publish(self.stream.take_pending())
            self.chunk_publisher.maybe_keepalive()
        await asyncio.sleep(0.05)

    async def collect(
        self, proc: subprocess.Popen[bytes]
    ) -> tuple[_ExecResult, ResultPayload | None]:
        """The exact close receipt and the result envelope of an owner that exited."""
        from ._subprocess import _read_result_envelope, _result_from_payload

        reader = self.reader
        if self.ready is None or proc.returncode != 0 or reader is None:
            if reader is not None:
                await reader.finish(max(0, self.bound - time.monotonic()))
            raise ResourceEvidenceError("owner exited without a successful exact close receipt")
        receipt = await asyncio.shield(self.attached_completion())
        self.settled = True
        if self.chunk_publisher is not None:
            self.chunk_publisher.publish(self.stream.take_pending())
        payload, error = _read_result_envelope(self.result, receipt.root_exit_code)
        return _result_from_payload(
            self.stream.getvalue(),
            payload,
            cancelled=self.cancelled,
            timed_out=receipt.reason == "timeout",
            envelope_error=error,
            stream_cap=self.stream.cap(),
            memory_guard_notice=read_notice(self.result),
        ), payload

    async def on_cancelled(self, original: asyncio.CancelledError) -> None:
        """EOF asks the independent owner to close; consume its exact terminal receipt first.

        Process mode has no hosted resource scope to retain a later consumer, so
        cancellation cannot propagate until this task has done so itself.
        """
        proc = self.proc
        if proc is not None and proc.stdin is not None:
            proc.stdin.close()
        if self.registration is not None and not self.attached:
            await self.settle_registration_after_cancel(self.registration, original)
        if self.attached:
            owner_completion = self.attached_completion()
            try:
                await self.finish_despite_cancellation(owner_completion)
            except Exception as cleanup:
                original.add_note(
                    "attached owner cancellation cleanup remains unresolved: "
                    f"{type(cleanup).__name__}: {cleanup}"
                )
            else:
                self.settled = True

    async def settle_registration_after_cancel(
        self, registration: asyncio.Task[None], original: asyncio.CancelledError
    ) -> None:
        try:
            await self.finish_despite_cancellation(registration)
        except ResourceEvidenceError:
            try:
                await self.settle_unpermitted_owner()
            except Exception as cleanup:
                original.add_note(
                    "unpermitted owner cancellation cleanup remains unresolved: "
                    f"{type(cleanup).__name__}: {cleanup}"
                )
            else:
                self.settled = True
                if self.scope is not None:
                    self.scope.complete(self.request, None)
        except Exception as cleanup:
            original.add_note(
                "exec registration outcome remains ambiguous after cancellation: "
                f"{type(cleanup).__name__}: {cleanup}"
            )
        else:
            self.attached = True
            if self.scope is not None:
                self.scope.unresolved[self.request] = self.ready

    async def on_refused(self, exc: ResourceEvidenceError) -> None:
        """Settle what a refusal left behind: nothing launched, or an unpermitted owner."""
        if self.proc is None and self.scope is not None:
            # A synchronous validation refusal rolled back before Popen. This
            # does not cover connection/commit ambiguity, which stays sticky.
            self.scope.complete(self.request, None)
        elif self.ready is not None and not self.attached:
            try:
                await self.settle_unpermitted_owner()
            except Exception as cleanup:
                exc.add_note(
                    "unpermitted owner closure remains unresolved: "
                    f"{type(cleanup).__name__}: {cleanup}"
                )
            else:
                if self.scope is not None:
                    self.scope.complete(self.request, None)

    def close_stdin(self) -> None:
        proc = self.proc
        if proc is not None and proc.stdin is not None and not proc.stdin.closed:
            proc.stdin.close()

    def needs_hand_off(self) -> bool:
        """Whether an attached owner is still unsettled while a hosted scope can retain it."""
        return not (
            self.settled
            or (
                not self.attached
                and not (self.registration is not None and not self.registration.done())
            )
            or self.proc is None
            or self.ready is None
            or self.reader is None
            or self.scope is None
        )

    async def finish_owner(self) -> None:
        # Preserve the original task's strong completion ownership. Host
        # cancellation does not mean the independent owner already closed.
        if self.registration is not None and not self.attached:
            try:
                await asyncio.shield(self.registration)
            except ResourceEvidenceError:
                await self.settle_unpermitted_owner()
                if self.scope is not None:
                    self.scope.complete(self.request, None)
                return
            self.attached = True
            if (
                self.scope is not None
                and self.request in self.scope.unresolved
                and self.scope.unresolved[self.request] is None
            ):
                self.scope.unresolved[self.request] = self.ready
        await asyncio.shield(self.attached_completion())

    def crashed(self, label: str, exc: Exception) -> tuple[_ExecCrashed, None]:
        return _ExecCrashed(output=f"{label}: {exc}\n{self.stream.getvalue()}", exc=exc), None


async def run_owned(
    db: Database,
    target: RuntimeIncarnation,
    code: str,
    context: AvaContext,
    cancel_event: asyncio.Event,
    timeout: float,
    chunk_publisher: ExecOutputChunkPublisher | None,
    *,
    accumulation_max_chars: int,
    state: dict[str, Any] | None,
    exec_dir: Path | None,
    config_overlay: dict[str, object] | None,
    birth_config: dict[str, object] | None,
) -> tuple[_ExecResult, ResultPayload | None]:
    """Run one managed exec with its invocation-owned output pipe and completion tasks."""
    owned = _OwnedRun(
        db,
        target,
        code,
        context,
        cancel_event,
        timeout,
        chunk_publisher,
        accumulation_max_chars=accumulation_max_chars,
        state=state,
        exec_dir=exec_dir,
    )

    primary: BaseException | None = None
    try:
        proc, launcher = owned.launch(config_overlay, birth_config)
        owned.reader = ExecOutputPipe(proc, owned.stream)
        owned.reader.watch()
        while proc.poll() is None:
            if owned.ready_pending():
                ready = owned.read_ready(launcher)
                await owned.finish_attach(owned.start_registration(ready), ready)
            await owned.tick()
        return await owned.collect(proc)
    except asyncio.CancelledError as original:
        primary = original
        await owned.on_cancelled(original)
        raise
    except ResourceEvidenceError as exc:
        primary = exc
        await owned.on_refused(exc)
        return owned.crashed("managed exec refused", exc)
    except Exception as exc:
        primary = exc
        return owned.crashed("managed exec remains unresolved", exc)
    finally:
        try:
            await owned.stop(max(0, owned.bound - time.monotonic()))
        except BaseException as cleanup:
            if primary is None:
                raise
            if cleanup is not primary:
                primary.add_note(
                    f"managed exec cleanup failed: {type(cleanup).__name__}: {cleanup}"
                )
        finally:
            if owned.needs_hand_off():
                assert owned.scope is not None  # noqa: S101
                owned.scope.require_service().complete_later(
                    owned.scope, owned.finish_owner(), name=f"exec-owner-close-{owned.request_id}"
                )
