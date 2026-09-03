"""Actual exec consumer for explicitly admitted durable resource sets.

Legacy NULL rows keep the existing protocol-zero path. No environment flag,
request label or installed revision enables managed resource authority.
"""

import asyncio
import hashlib
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import psutil

from agent.graph._exec_protocol import KILL_GRACE_S, ResultPayload, write_request
from agent.graph._exec_result import _ExecCrashed, _ExecResult
from agent.graph._exec_stream import ExecOutputChunkPublisher, StreamingTextIO
from shared.db_transaction import write_transaction
from shared.exec_owner_protocol import (
    OwnerClosed,
    OwnerContext,
    OwnerControl,
    OwnerReady,
    publish_owner_message,
    read_owner_bytes,
)
from shared.incarnation_resources import (
    ExecAllocation,
    IncarnationResources,
    ResourceEvidenceError,
    attach_exec,
    complete_exec,
    decode_resources,
    register_exec,
)
from shared.paths import exec_run_dir
from shared.runtime_incarnation import RuntimeIncarnation, current_incarnation
from shared.turn_identity import current_hosted_resources


def managed_target(agent_id: int | None) -> RuntimeIncarnation | None:
    if agent_id is None:
        return None
    target = current_incarnation(agent_id)
    if target is None:
        return None
    with write_transaction() as conn:
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


def _reserve(context: OwnerContext) -> None:
    with write_transaction() as conn:
        register_exec(
            conn,
            RuntimeIncarnation(context.agent_id, context.generation, context.runtime_owner),
            context.allocation,
        )


def _attach(context: OwnerContext, ready: OwnerReady) -> None:
    with write_transaction() as conn:
        attach_exec(
            conn,
            RuntimeIncarnation(context.agent_id, context.generation, context.runtime_owner),
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


def _complete(context: OwnerContext, attached: ExecAllocation) -> None:
    with write_transaction() as conn:
        complete_exec(
            conn,
            RuntimeIncarnation(context.agent_id, context.generation, context.runtime_owner),
            attached,
        )


async def run_owned(  # noqa: PLR0915 -- one caller retains exact allocation and subprocess ownership.
    target: RuntimeIncarnation,
    code: str,
    cancel_event: asyncio.Event,
    timeout: float,
    chunk_publisher: ExecOutputChunkPublisher | None,
    *,
    state: dict[str, Any] | None,
    exec_dir: Path | None,
    config_overlay: dict[str, object] | None,
    birth_config: dict[str, object] | None,
) -> tuple[_ExecResult, ResultPayload | None]:
    from agent.graph._exec_subprocess import (
        _build_child_env,
        _drain_output,
        _read_result_envelope,
        _result_from_payload,
    )

    request_id = uuid4()
    directory = (
        (exec_dir or exec_run_dir()) / str(target.agent_id) / "domains" / str(request_id)
    ).resolve()
    directory.mkdir(parents=True, mode=0o700)
    request = directory / f"req-{request_id.hex}.json"
    result = directory / "result.json"
    context_path = directory / "owner.json"
    deadline = datetime.now(UTC) + timedelta(seconds=timeout)
    write_request(request, code=code, agent_id=target.agent_id, timeout_s=timeout, state=state)
    allocation = ExecAllocation(
        request=request_id,
        domain=uuid4(),
        request_digest=hashlib.sha256(request.read_bytes()).hexdigest(),
        deadline=deadline,
    )
    context = OwnerContext(
        agent_id=target.agent_id,
        generation=target.generation,
        runtime_owner=target.owner,
        request_path=request,
        result_path=result,
        allocation=allocation,
    )
    publish_owner_message(context_path, context)
    scope = current_hosted_resources()
    if scope is not None:
        scope.unresolved[request] = None
    stream = StreamingTextIO()
    proc: subprocess.Popen[bytes] | None = None
    ready: OwnerReady | None = None
    reader: threading.Thread | None = None
    cancelled = False
    settled = False
    attached = False
    bound = time.monotonic() + timeout + KILL_GRACE_S

    def send(action: Literal["permit", "cancel"]) -> None:
        if proc is None or proc.stdin is None or proc.stdin.closed:
            return
        message = OwnerControl(request=request_id, domain=allocation.domain, action=action)
        proc.stdin.write(message.model_dump_json().encode() + b"\n")
        proc.stdin.flush()

    try:
        await asyncio.to_thread(_reserve, context)
        env = _build_child_env(
            target.agent_id,
            request,
            result,
            config_overlay=config_overlay,
            birth_config=birth_config,
        )
        proc = subprocess.Popen(  # noqa: S603 -- fixed isolated owner entry, inherited prepared runtime.
            [
                sys.executable,
                "-I",
                "-X",
                "utf8",
                "-m",
                "agent.exec_domain_owner",
                "--context",
                str(context_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            close_fds=True,
        )
        native = psutil.Process(proc.pid)
        birth = native.create_time()
        reader = threading.Thread(target=_drain_output, args=(proc, stream), daemon=True)
        reader.start()
        while proc.poll() is None:
            if ready is None and context_path.with_suffix(".ready").exists():
                ready = OwnerReady.model_validate_json(
                    read_owner_bytes(context_path.with_suffix(".ready"))
                )
                owner, root = ready.allocation.owner_process, ready.allocation.root_process
                if owner is None or root is None or (owner.pid, owner.birth) != (proc.pid, birth):
                    raise ResourceEvidenceError("ready receipt does not identify the actual owner")  # noqa: TRY301 -- retain allocation and close the control pipe.
                child = psutil.Process(root.pid)
                if child.create_time() != root.birth or child.ppid() != proc.pid:
                    raise ResourceEvidenceError("ready root is not the owner's exact child")  # noqa: TRY301 -- retain allocation and close the control pipe.
                await asyncio.to_thread(_attach, context, ready)
                attached = True
                if scope is not None:
                    scope.unresolved[request] = ready
                send("permit")
            if cancel_event.is_set() and not cancelled:
                cancelled = True
                send("cancel")
            if time.monotonic() >= bound:
                raise ResourceEvidenceError("owner did not settle within the original exec bound")  # noqa: TRY301 -- uncertainty remains durable.
            await asyncio.sleep(0.05)
        if ready is None or proc.returncode != 0:
            raise ResourceEvidenceError("owner exited without a successful exact close receipt")  # noqa: TRY301 -- uncertainty remains durable.
        receipt = validate_closed(context, ready.allocation, context_path.with_suffix(".closed"))
        await asyncio.to_thread(reader.join, max(0, bound - time.monotonic()))
        if reader.is_alive():
            raise ResourceEvidenceError("owner output reader remains unresolved")  # noqa: TRY301 -- do not clear allocation.
        await asyncio.to_thread(_complete, context, ready.allocation)
        settled = True
        if scope is not None:
            scope.complete(request, ready)
        if chunk_publisher is not None:
            chunk_publisher.publish(stream.getvalue())
        payload, error = _read_result_envelope(result, receipt.root_exit_code)
        return _result_from_payload(
            stream.getvalue(),
            payload,
            cancelled=cancelled,
            timed_out=receipt.reason == "timeout",
            envelope_error=error,
            stream_cap=stream.cap(),
        ), payload
    except asyncio.CancelledError:
        # EOF closes the independently owned domain; it does not clear evidence.
        if proc is not None and proc.stdin is not None:
            proc.stdin.close()
        raise
    except ResourceEvidenceError as exc:
        if proc is None and scope is not None:
            # A synchronous validation refusal rolled back before Popen. This
            # does not cover connection/commit ambiguity, which stays sticky.
            scope.complete(request, None)
        return _ExecCrashed(
            output=f"managed exec refused: {exc}\n{stream.getvalue()}", exc=exc
        ), None
    except Exception as exc:
        return _ExecCrashed(
            output=f"managed exec remains unresolved: {exc}\n{stream.getvalue()}", exc=exc
        ), None
    finally:
        if proc is not None and proc.stdin is not None and not proc.stdin.closed:
            proc.stdin.close()
        if (
            not settled
            and attached
            and proc is not None
            and ready is not None
            and reader is not None
            and scope is not None
        ):
            # Preserve the original task's strong completion ownership. Host
            # cancellation does not mean the independent owner already closed.
            async def finish_owner() -> None:
                try:
                    code = await asyncio.to_thread(proc.wait, max(0.001, bound - time.monotonic()))
                    if code != 0:
                        return
                    await asyncio.to_thread(
                        validate_closed,
                        context,
                        ready.allocation,
                        context_path.with_suffix(".closed"),
                    )
                    await asyncio.to_thread(reader.join, max(0, bound - time.monotonic()))
                    if reader.is_alive():
                        return
                    await asyncio.to_thread(_complete, context, ready.allocation)
                    scope.complete(request, ready)
                except Exception as exc:
                    from shared.log import logger

                    logger.error("exec owner remains unresolved: {error}", error=exc)

            task = asyncio.create_task(finish_owner(), name=f"exec-owner-close-{request_id}")
            scope.completions.add(task)
            task.add_done_callback(scope.completions.discard)
