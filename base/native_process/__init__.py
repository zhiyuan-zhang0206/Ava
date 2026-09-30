"""Standard-library process and runtime identity — the stdlib floor below `log`.

Everything a process can know about itself without Settings, a database read,
or a session backend: OS platform flags (`os_platform`), native birth facts
(PID plus kernel start ticks in an explicit boot scope), the commit a
*running* process actually loaded (`loaded_commit`, as opposed to what the
checkout is at right now) and the ordered code version derived from it
(`code_version`), turn- and incarnation-scoped agent identity
(`turn_identity`, `runtime_incarnation`), one bounded POSIX command's process
group (`posix_command`, `group_closure`), managed-exec-domain ownership without the agent graph
(`exec_domain`), child-process environment primitives (`child_env`), strict
process-observation value types (`evidence`), and the root supervisor's local
control contract (`root_control`). Moving the commit, platform and identity
primitives here — rather than leaving them beside the sessions that use them —
is what lets `log` (which stamps commit and agent id on every record) and
every native session host sit above this package without import cycles.

Linux birth identity is PID plus kernel start ticks in an explicit boot scope.
Wall-derived timestamps remain diagnostic. Serialized receipts retain all fields;
only independent observations compare through process_birth_key.
"""

from __future__ import annotations

import subprocess
import sys
from functools import cache
from pathlib import Path
from uuid import UUID


def pid_starttime_ticks(pid: int) -> int | None:
    """Linux `/proc` start time in clock ticks since boot, or None when unavailable.

    The command name in field 2 may include spaces or parentheses, so split the
    stat record only after its final closing parenthesis.
    """
    try:
        line = Path(f"/proc/{pid}/stat").read_text()
        rest = line.rsplit(")", 1)[1].split()
        ticks = int(rest[22 - 3])
        return ticks if ticks > 0 else None
    except (IndexError, OSError, ValueError):
        return None


@cache
def native_boot_id() -> str | None:
    """Bind durable POSIX process custody to one native boot, without Settings.

    A running observer cannot survive reboot, so its boot scope is immutable.
    Missing or malformed POSIX evidence refuses; it is never a legacy default.
    """
    if sys.platform == "linux":
        value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    elif sys.platform == "darwin":
        value = subprocess.check_output(
            ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"], text=True, timeout=5
        ).strip()
    else:
        raise RuntimeError(f"unsupported native boot identity platform: {sys.platform}")
    return str(UUID(value))


def process_birth_key(
    pid: int, birth: float, starttime: int | None, *, platform: str
) -> tuple[int, str, int | float]:
    """The exact native key; Linux never substitutes a wall-clock timestamp."""
    if starttime is not None:
        if type(starttime) is not int or starttime <= 0:
            raise ValueError("native start ticks must be a positive integer")
        return pid, "ticks", starttime
    if platform == "linux":
        raise RuntimeError(f"missing Linux start ticks for PID {pid}")
    return pid, "timestamp", birth
