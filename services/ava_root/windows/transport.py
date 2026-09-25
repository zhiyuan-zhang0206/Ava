"""Owner-only, local named pipe carrying the root's bounded JSON-line protocol.

One pipe instance serializes exchanges; nonblocking native operations bound every
read/write without abandoned worker threads. Clients verify the connected native
server PID when reading root status. No TCP port, bearer file or desktop helper.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import os
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from ctypes import wintypes
from pathlib import Path
from typing import Any

from services.ava_root.ipc import MAX_MESSAGE_BYTES
from services.ava_root.windows.native import DWORD, private_security
from shared.winjob import _get_last_error, _kernel32, _last_error

_POLL_S = 0.01
_EXCHANGE_TIMEOUT_S = 30.0


def pipe_name(path: Path) -> str:
    identity = os.path.normcase(str(path.resolve())).encode()
    return rf"\\.\pipe\ava-root-{hashlib.sha256(identity).hexdigest()}"


def _api() -> Any:
    api = _kernel32()
    api.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR,
        DWORD,
        DWORD,
        DWORD,
        DWORD,
        DWORD,
        DWORD,
        ctypes.c_void_p,
    ]
    api.CreateNamedPipeW.restype = wintypes.HANDLE
    api.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        DWORD,
        DWORD,
        ctypes.c_void_p,
        DWORD,
        DWORD,
        wintypes.HANDLE,
    ]
    api.CreateFileW.restype = wintypes.HANDLE
    api.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    api.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
    api.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        DWORD,
        ctypes.POINTER(DWORD),
        ctypes.c_void_p,
    ]
    api.WriteFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        DWORD,
        ctypes.POINTER(DWORD),
        ctypes.c_void_p,
    ]
    api.SetNamedPipeHandleState.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(DWORD),
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    api.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(DWORD)]
    return api


def _read(handle: int) -> bytes:
    buffer = ctypes.create_string_buffer(MAX_MESSAGE_BYTES + 1)
    count = DWORD()
    if not _api().ReadFile(wintypes.HANDLE(handle), buffer, len(buffer), ctypes.byref(count), None):
        code = _get_last_error()
        if code == 232:  # ERROR_NO_DATA: nonblocking pipe has no bytes yet.
            return b""
        raise _last_error("read root pipe", code)
    return buffer.raw[: count.value]


def _write(handle: int, data: bytes) -> int:
    count = DWORD()
    buffer = ctypes.create_string_buffer(data)
    if not _api().WriteFile(wintypes.HANDLE(handle), buffer, len(data), ctypes.byref(count), None):
        code = _get_last_error()
        if code == 232:
            return 0
        raise _last_error("write root pipe", code)
    return int(count.value)


def _line(buffer: bytearray) -> bytes | None:
    if len(buffer) > MAX_MESSAGE_BYTES:
        raise ValueError("root pipe message exceeds protocol limit")
    if b"\n" in buffer:
        return bytes(buffer.split(b"\n", 1)[0])
    return None


def _deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("root pipe exchange deadline expired")


def roundtrip(path: Path, payload: bytes, timeout: float) -> tuple[bytes, int]:
    deadline = time.monotonic() + timeout
    api = _api()
    handle = None
    while handle is None:
        _deadline(deadline)
        candidate = api.CreateFileW(pipe_name(path), 0xC0000000, 0, None, 3, 0x00100000, None)
        if candidate != wintypes.HANDLE(-1).value:
            handle = int(candidate)
        elif _get_last_error() not in {2, 231, 232}:
            raise _last_error("connect root pipe")
        else:
            time.sleep(_POLL_S)
    try:
        mode, peer = DWORD(1), DWORD()
        if not api.SetNamedPipeHandleState(wintypes.HANDLE(handle), ctypes.byref(mode), None, None):
            raise _last_error("make root pipe nonblocking")
        if not api.GetNamedPipeServerProcessId(wintypes.HANDLE(handle), ctypes.byref(peer)):
            raise _last_error("read root pipe server identity")
        remaining = payload
        while remaining:
            _deadline(deadline)
            remaining = remaining[_write(handle, remaining) :]
            if remaining:
                time.sleep(_POLL_S)
        buffer = bytearray()
        while True:
            _deadline(deadline)
            buffer.extend(_read(handle))
            line = _line(buffer)
            if line is not None:
                while not _write(handle, b"\0"):
                    _deadline(deadline)
                    time.sleep(_POLL_S)
                return line, int(peer.value)
            time.sleep(_POLL_S)
    finally:
        api.CloseHandle(wintypes.HANDLE(handle))


class PipeServer:
    def __init__(
        self,
        path: Path,
        handler: Callable[[bytes], Awaitable[bytes]],
        after: Callable[[bytes], None],
    ) -> None:
        self._path, self._handler, self._after = path, handler, after
        self._handle: int | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        with private_security() as security:
            # FIRST_PIPE_INSTANCE refuses a squatter; REJECT_REMOTE_CLIENTS is native.
            handle = _api().CreateNamedPipeW(
                pipe_name(self._path),
                0x00080003,
                9,
                1,
                MAX_MESSAGE_BYTES,
                MAX_MESSAGE_BYTES,
                0,
                ctypes.byref(security),
            )
        if handle == wintypes.HANDLE(-1).value:
            raise _last_error("create private root pipe")
        self._handle = int(handle)
        self._task = asyncio.create_task(self._serve())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._handle is not None:
            _api().CloseHandle(wintypes.HANDLE(self._handle))
            self._handle = None

    async def _serve(self) -> None:
        if self._handle is None:
            raise RuntimeError("root pipe has no native handle")
        handle = self._handle
        while True:
            accepted = _api().ConnectNamedPipe(wintypes.HANDLE(handle), None)
            code = 0 if accepted else _get_last_error()
            if code in {0, 535}:  # ERROR_PIPE_CONNECTED
                try:
                    await self._exchange(handle)
                except (OSError, ValueError):
                    pass  # Malformed or disconnected peers cannot stop the root.
                finally:
                    _api().DisconnectNamedPipe(wintypes.HANDLE(handle))
            elif code == 232:
                _api().DisconnectNamedPipe(wintypes.HANDLE(handle))
            elif code != 536:  # PIPE_LISTENING
                raise _last_error("accept root pipe", code)
            await asyncio.sleep(_POLL_S)

    async def _exchange(self, handle: int) -> None:
        deadline = time.monotonic() + _EXCHANGE_TIMEOUT_S
        buffer = bytearray()
        while True:
            _deadline(deadline)
            buffer.extend(_read(handle))
            raw = _line(buffer)
            if raw is not None:
                break
            await asyncio.sleep(_POLL_S)
        response = await self._handler(raw)
        if len(response) > MAX_MESSAGE_BYTES:
            raise ValueError("root response exceeds protocol limit")
        remaining = response
        while remaining:
            _deadline(deadline)
            remaining = remaining[_write(handle, remaining) :]
            if remaining:
                await asyncio.sleep(_POLL_S)
        # DisconnectNamedPipe discards unread bytes. A transport acknowledgement
        # proves the response was read without an unbounded FlushFileBuffers.
        while _read(handle) != b"\0":
            _deadline(deadline)
            await asyncio.sleep(_POLL_S)
        self._after(raw)
