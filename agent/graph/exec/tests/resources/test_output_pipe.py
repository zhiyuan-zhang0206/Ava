"""Real descriptor boundaries for invocation-owned nonblocking output."""

import asyncio
import os
from types import SimpleNamespace
from typing import Any

import pytest

from agent.graph.exec._output_pipe import ExecOutputPipe
from agent.graph.exec._stream import StreamingTextIO


def pipe() -> tuple[ExecOutputPipe, int]:
    source, destination = os.pipe()
    process: Any = SimpleNamespace(stdout=os.fdopen(source, "rb"))
    return ExecOutputPipe(process, StreamingTextIO(max_chars=64)), destination


async def test_partial_output_streams_before_writer_eof() -> None:
    reader, destination = pipe()
    try:
        os.write(destination, b"partial before exit\xff")
        reader.pump()
        assert reader.stream.getvalue() == "partial before exit\ufffd"
        assert not reader.closed
        os.write(destination, b" final tail")
        os.close(destination)
        destination = -1
        await reader.finish(1)
        assert reader.closed
        assert reader.stream.getvalue() == "partial before exit\ufffd final tail"
    finally:
        reader.close()
        if destination != -1:
            os.close(destination)


async def test_readiness_drains_output_between_invocation_poll_beats() -> None:
    reader, destination = pipe()
    reader.watch()
    try:
        os.write(destination, b"ready")
        for _ in range(10):
            if reader.stream.getvalue():
                break
            await asyncio.sleep(0)
        assert reader.stream.getvalue() == "ready"
        assert not reader.closed
    finally:
        os.close(destination)
        reader.close()


async def test_readiness_error_is_returned_to_the_actual_tail_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, destination = pipe()
    original = RuntimeError("stream failed")

    def failed(_text: str) -> int:
        raise original

    monkeypatch.setattr(reader.stream, "write", failed)
    reader.watch()
    try:
        os.write(destination, b"error")
        for _ in range(10):
            if reader.error is not None:
                break
            await asyncio.sleep(0)
        assert reader.closed
        with pytest.raises(RuntimeError) as caught:
            await reader.finish(1)
        assert caught.value is original
    finally:
        os.close(destination)
        reader.close()


async def test_tail_timeout_keeps_actual_pipe_unresolved_until_late_eof() -> None:
    reader, destination = pipe()
    try:
        started = asyncio.get_running_loop().time()
        await reader.finish(0.02)
        assert asyncio.get_running_loop().time() - started < 0.5
        assert not reader.closed
        os.write(destination, b"late")
        os.close(destination)
        destination = -1
        await reader.finish(1)
        assert reader.closed
        assert reader.stream.getvalue() == "late"
    finally:
        reader.close()
        if destination != -1:
            os.close(destination)


def test_emergency_tail_does_not_need_an_event_loop() -> None:
    reader, destination = pipe()
    os.write(destination, b"tail")
    os.close(destination)
    try:
        reader.finish_now(1)
        assert reader.closed
        assert reader.stream.getvalue() == "tail"
    finally:
        reader.close()


async def test_output_cap_does_not_prevent_eof_settlement() -> None:
    reader, destination = pipe()
    os.write(destination, b"a" * 1024)
    os.close(destination)
    try:
        await reader.finish(1)
        assert reader.closed
        assert reader.stream.getvalue().startswith("a" * 32)
        assert reader.stream.getvalue().endswith("a" * 32)
        assert "960 chars dropped" in reader.stream.getvalue()
        assert reader.stream.cap() is not None
    finally:
        reader.close()


def test_missing_pipe_fails_at_launch_boundary() -> None:
    process: Any = SimpleNamespace(stdout=None)
    with pytest.raises(RuntimeError, match="no output pipe"):
        ExecOutputPipe(process, StreamingTextIO(max_chars=64))
