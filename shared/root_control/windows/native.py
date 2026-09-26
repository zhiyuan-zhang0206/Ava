"""Win32 handles and current-user security descriptors; no settings or effects at import."""

from __future__ import annotations

import ctypes
from collections.abc import Generator
from contextlib import contextmanager
from ctypes import wintypes
from typing import Any, cast

from shared.winjob import _kernel32, _last_error

DWORD = ctypes.c_uint32


class SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", DWORD), ("descriptor", ctypes.c_void_p), ("inherit", ctypes.c_int32)]


def _security_api() -> Any:
    loader = cast(Any, ctypes).WinDLL
    api = loader("advapi32", use_last_error=True)
    api.OpenProcessToken.argtypes = [wintypes.HANDLE, DWORD, ctypes.POINTER(wintypes.HANDLE)]
    api.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        DWORD,
        ctypes.POINTER(DWORD),
    ]
    api.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    return api


def current_user_sid() -> str:
    """Read the actual token user, independent of username/environment spellings."""
    kernel = _kernel32()
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    api = _security_api()
    token = wintypes.HANDLE()
    if not api.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise _last_error("OpenProcessToken")
    try:
        size = DWORD()
        api.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not api.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise _last_error("GetTokenInformation")
        sid = ctypes.c_void_p.from_buffer(buffer)
        text = wintypes.LPWSTR()
        if not api.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise _last_error("ConvertSidToStringSidW")
        try:
            return text.value or ""
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.CloseHandle(token)


def command_argv(command: str) -> list[str]:
    """Use the native Windows quote/backslash rules, never POSIX shlex."""
    if not command.strip() or "\0" in command:
        raise ValueError("empty or NUL-containing Windows command")
    api = cast(Any, ctypes).WinDLL("shell32", use_last_error=True)
    api.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    api.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    count = ctypes.c_int()
    values = api.CommandLineToArgvW(command.lstrip(), ctypes.byref(count))
    if not values:
        raise _last_error("CommandLineToArgvW")
    try:
        return [str(values[index]) for index in range(count.value)]
    finally:
        kernel = _kernel32()
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(values)


@contextmanager
def private_security() -> Generator[SecurityAttributes]:
    """Protected DACL permits this user and LocalSystem; handles never inherit."""
    sid = current_user_sid()
    if not sid:
        raise RuntimeError("cannot establish the native token user")
    descriptor = ctypes.c_void_p()
    if not _security_api().ConvertStringSecurityDescriptorToSecurityDescriptorW(
        f"O:{sid}D:P(A;;GA;;;{sid})(A;;GA;;;SY)", 1, ctypes.byref(descriptor), None
    ):
        raise _last_error("create owner-only security descriptor")
    try:
        yield SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor.value, 0)
    finally:
        _kernel32().LocalFree(descriptor)
