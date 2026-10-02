"""The operating system's own memory-pressure state and per-process footprint.

Two reads, both stateless: `pressure()` is the level the kernel itself reports
(no threshold of ours is involved), `footprint(pid)` is the memory the kernel
charges to one process. macOS only today: `kern.memorystatus_vm_pressure_level`
(1 normal, 2 warning, 4 critical) and `proc_pid_rusage`'s `ri_phys_footprint`,
which — unlike the RSS `ps` shows — counts compressed and swapped pages. Linux
has no equivalent state-typed signal (PSI and cgroup events are rates and
counters), so `host_memory_source()` returns None there and nothing acts on it.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import struct
import sys
from typing import Literal, Protocol

PressureLevel = Literal["normal", "warning", "critical"]

_DARWIN_LEVELS: dict[int, PressureLevel] = {1: "normal", 2: "warning", 4: "critical"}
_RUSAGE_INFO_V2 = 2
# `struct rusage_info_v2`: a 16-byte uuid, then u64 counters; `ri_phys_footprint`
# is the 8th of them.
_PHYS_FOOTPRINT_OFFSET = 16 + 7 * 8
_RUSAGE_BUFFER_BYTES = 512


class MemorySource(Protocol):
    """What the memory guard reads; tests substitute a fake."""

    def pressure(self) -> PressureLevel: ...

    def footprint(self, pid: int) -> int:
        """Bytes the kernel charges to `pid`; 0 when the process is gone."""
        ...


class _DarwinMemory:
    def pressure(self) -> PressureLevel:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        value = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(value))
        name = b"kern.memorystatus_vm_pressure_level"
        if libc.sysctlbyname(name, ctypes.byref(value), ctypes.byref(size), None, 0) != 0:
            raise OSError(ctypes.get_errno(), f"sysctl {name.decode()} failed")
        return _DARWIN_LEVELS[value.value]

    def footprint(self, pid: int) -> int:
        libproc = ctypes.CDLL(ctypes.util.find_library("proc"), use_errno=True)
        buffer = ctypes.create_string_buffer(_RUSAGE_BUFFER_BYTES)
        if libproc.proc_pid_rusage(pid, _RUSAGE_INFO_V2, buffer) != 0:
            if ctypes.get_errno() == errno.ESRCH:
                return 0
            raise OSError(ctypes.get_errno(), f"proc_pid_rusage({pid}) failed")
        return struct.unpack_from("<Q", buffer, _PHYS_FOOTPRINT_OFFSET)[0]


def host_memory_source() -> MemorySource | None:
    """The platform's memory source, or None where the OS exposes no pressure state."""
    return _DarwinMemory() if sys.platform == "darwin" else None
