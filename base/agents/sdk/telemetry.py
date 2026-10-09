"""SDK-call events and optional per-execution tallies.

Every wrapped public SDK entry emits by default, including nested, external Python
and framework calls. Live sampling policy affects events only. The execution owner
passes its optional full tally explicitly; it never gates instrumentation.
Each call retains the explicit caller-identity snapshot its recorder supplied.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    # Annotation-only (`sdk_calls_by_tool_call_id`); the runtime isinstance
    # imports ToolMessage at its call site. This module loads on the exec-child
    # boot path (`_run_code`), which must stay off the LangChain message stack
    # (task #3633; `_TYPE_CHECKING_ALLOWED`).
    from langchain_core.messages import BaseMessage

from base.agents.messages.kwargs import AvaMsgType, read_ava_kwargs
from base.agents.sdk.call_policy import SamplingPolicy
from base.agents.sdk.tally import SdkCallTally

# Event name written to events for each public SDK entry.
SDK_CALL_EVENT = "sdk_call"


def emit(
    fn: str,
    detail: Mapping[str, Any] | None = None,
    duration: float | None = None,
    *,
    identity: Mapping[str, Any],
    sampling_policy: SamplingPolicy | None = None,
) -> None:
    """Write one ``sdk_call`` event; invalid input and emitter errors propagate.
    ``detail`` is omitted from the payload when empty, so a plain call stays ``{fn}``;
    ``duration`` (seconds, measured by ``run_metered``) rides as a top-level payload
    key — the registry declares it (``contract.SdkCall``), so a reader may reference
    ``attributes->>'duration'``. Policy errors propagate before emission; metered
    calls supply their entry snapshot so a refresh cannot mask the SDK outcome."""
    from base.agents.sdk.call_policy import policy

    current = policy() if sampling_policy is None else sampling_policy
    every = current.sample_every if current.sampling_enabled else 1
    if every > 1:
        import random

        if random.randrange(every) != 0:  # noqa: S311 — telemetry sampling, not security
            return
    extra: dict[str, Any] = {"fn": fn, "sample_rate": every}
    if detail:
        extra["detail"] = dict(detail)
    if duration is not None:
        extra["duration"] = duration
    from base import telemetry

    telemetry.emit("telemetry", SDK_CALL_EVENT, attributes=extra, **identity)


@contextlib.contextmanager
def _event_capture_admission() -> Generator[Callable[[], None] | None, None, None]:
    """Use the optional gate; invalid capture code rejects the SDK call before its body."""
    from base.agents.impersonation.manifest import admitted_local_sdk_call

    with admitted_local_sdk_call() as admission:
        yield None if admission is None else admission.capture_failed


@contextlib.contextmanager
def _measure(
    fn: str, identity: Mapping[str, Any], tally: SdkCallTally | None
) -> Generator[None, None, None]:
    from base.agents.sdk.call_policy import policy

    snapshot = policy()
    caller_identity = dict(identity)
    # Retain this call's original gate until its event is captured. Attachment
    # close can reject new entries but cannot seal this receipt before drain.
    with _event_capture_admission() as capture_failed:
        t0 = time.monotonic()
        primary: BaseException | None = None
        try:
            yield
        except BaseException as exc:
            primary = exc
            raise
        finally:
            if tally is not None:
                tally.add(fn)
            try:
                emit(
                    fn,
                    duration=time.monotonic() - t0,
                    identity=caller_identity,
                    sampling_policy=snapshot,
                )
            except BaseException as secondary:
                if capture_failed is not None:
                    try:
                        capture_failed()
                    except BaseException as capture_error:
                        secondary.add_note(
                            f"Receipt failure recording also failed: {capture_error!r}"
                        )
                if primary is None:
                    raise
                primary.add_note(f"SDK call {fn!r} event emission also failed: {secondary!r}")
                for note in getattr(secondary, "__notes__", ()):
                    primary.add_note(note)


def run_metered(
    fn: str,
    original: Callable[..., Any],
    args: Any,
    kwargs: Any,
    *,
    identity: Mapping[str, Any],
    tally: SdkCallTally | None = None,
) -> Any:
    """Validate each public entry before execution and retain its call-local snapshots."""
    with _measure(fn, identity, tally):
        return original(*args, **kwargs)


async def run_metered_async(
    fn: str,
    original: Callable[..., Awaitable[Any]],
    args: Any,
    kwargs: Any,
    *,
    identity: Mapping[str, Any],
    tally: SdkCallTally | None = None,
) -> Any:
    """Validate when awaited, preserving cancellation and the call's own admission."""
    with _measure(fn, identity, tally):
        return await original(*args, **kwargs)


# ── the wire-side counts: entry model + materialization + read-back ───────────


class SdkCall(BaseModel):
    """One SDK method's call count in an agent_code block, e.g.
    ``SdkCall(method="files.read", count=3)`` for ``ava.files.read(...)`` x3."""

    method: str
    count: int


def tally_entries(tally: Mapping[str, int]) -> list[dict[str, Any]]:
    """Materialize a recording tally as the wire list that rides the exec result:
    ``[{"method": "<ns>.<fn>", "count": N}, ...]``, sorted by descending count then
    method — the collapsed-code chip's render order (JSON dicts, one per fn)."""
    entries: list[dict[str, Any]] = []
    for fn, count in sorted(tally.items(), key=lambda item: (-item[1], item[0])):
        entries.append({"method": fn, "count": count})
    return entries


def sdk_calls_by_tool_call_id(
    messages: Sequence[BaseMessage], start: int = 0
) -> dict[str, list[SdkCall]]:
    """Map each exec_output ToolMessage's ``tool_call_id`` to its block's SDK calls.

    The counts are the runtime tally ``agent/graph/exec/node.py`` wrote to the message's
    ``additional_kwargs["sdk_calls"]`` — what the code really executed, never a scan
    of its text. Only ``messages[start:]`` is scanned (the rendered span): a tool
    call's metadata rides the ToolMessage that follows it, so nothing outside the
    span can enrich an item rendered from it. A message without the field (pre-tally
    history) stays absent from the map; a block that ran with zero SDK calls maps to
    ``[]`` — a real zero the UI must not confuse with "not yet known".
    """
    from langchain_core.messages import ToolMessage

    out: dict[str, list[SdkCall]] = {}
    for msg in messages[start:]:
        if not isinstance(msg, ToolMessage):
            continue
        kwargs = read_ava_kwargs(msg)
        if kwargs.get("ava_msg_type") != AvaMsgType.EXEC_OUTPUT:
            continue
        calls = kwargs.get("sdk_calls")
        if calls is None:
            continue
        out[msg.tool_call_id] = [SdkCall.model_validate(entry) for entry in calls]
    return out
