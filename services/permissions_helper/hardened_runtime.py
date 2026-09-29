"""The helper's hardened-runtime gate: how it is signed and how that is read back.

Signed with the hardened runtime (no dyld or library-validation exception),
dyld ignores every DYLD_* variable for the helper and maps only platform
libraries, so no injected code runs before `main` in the permission ancestor or
the release custodian. Without it an inserted library's constructor runs first
while the running image still satisfies `codesign -R`. The designated
requirement, and the TCC grants keyed on it, do not change.
"""

from __future__ import annotations

import re

SIGNING_OPTIONS = ("--options", "runtime")
CS_VALID = 0x1
CS_RUNTIME = 0x10000
_CS_OPS_STATUS = 0
_CODE_DIRECTORY_FLAGS = re.compile(
    r"^CodeDirectory v=\S+ size=\d+ flags=0x([0-9a-f]+)\(", re.MULTILINE
)


def code_directory_flags(display: str) -> int | None:
    """The signed file's flags from `codesign --display --verbose=2` output."""
    match = _CODE_DIRECTORY_FLAGS.search(display)
    return None if match is None else int(match.group(1), 16)


def running_code_flags(pid: int) -> int:
    """The kernel's current code-signing status of a running process (`csops`)."""
    import ctypes

    libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    csops = libsystem.csops
    csops.argtypes = (ctypes.c_int, ctypes.c_uint, ctypes.c_void_p, ctypes.c_size_t)
    csops.restype = ctypes.c_int
    flags = ctypes.c_uint32()
    if csops(pid, _CS_OPS_STATUS, ctypes.byref(flags), ctypes.sizeof(flags)) != 0:
        reason = ctypes.get_errno()
        raise RuntimeError(f"cannot read the code-signing status of {pid}: errno {reason}")
    return flags.value


def running_hardened(pid: int) -> bool:
    """Valid and hardened now, so dyld ignored any DYLD_* injection at its start."""
    return running_code_flags(pid) & (CS_VALID | CS_RUNTIME) == CS_VALID | CS_RUNTIME
