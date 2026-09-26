"""Fail-closed reader for one launchd job's ``launchctl print`` text.

Apple's launchctl(1) says ``print`` output is not an API. This module is an
explicit, version-bound contract instead of a stable-interface claim: it admits
only the structure and fields measured natively on the supported macOS family
and refuses everything else, so an unknown format retains custody rather than
becoming evidence. Nested coalition counters are never read as job facts.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Literal

from pydantic import Field

from cli.release_transition.request import Record

# Measured on macOS 26.6.2 (25G83), Darwin 25.6.0, arm64, GUI domain gui/501.
SUPPORTED_PRODUCT_MAJORS = frozenset({"26"})

_BLOCKS = frozenset(
    {
        "arguments",
        "inherited environment",
        "default environment",
        "environment",
        "resource coalition",
        "jetsam coalition",
    }
)
_CRITICAL = (
    "active count",
    "path",
    "type",
    "state",
    "program",
    "working directory",
    "stdout path",
    "stderr path",
    "domain",
    "umask",
    "asid",
    "exit timeout",
    "runs",
    "properties",
)
_DIAGNOSTIC = frozenset(
    {
        "minimum runtime",
        "immediate reason",
        "forks",
        "execs",
        "initialized",
        "trampolined",
        "started suspended",
        "proxy started suspended",
        "checked allocations",
        "checked allocations reason",
        "checked allocations flags",
        "spawn type",
        "jetsam priority",
        "jetsam memory limit (active)",
        "jetsam memory limit (inactive)",
        "jetsamproperties category",
        "jetsam thread limit",
        "cpumon",
        # Present after a crash signal (measured: SIGTRAP from a Swift trap).
        "successive crashes",
    }
)
_OUTCOME = frozenset({"pid", "last exit code", "last terminating signal"})
_EXIT_CODE = re.compile(r"(\d{1,3})(?:: [A-Z][A-Z_]*)?")
_DOMAIN = re.compile(r"gui/(\d+) \[(\d+)\]")
# launchd prints a terminating signal as Darwin's strsignal(3) text, which
# already ends in ": <number>". Measured on 26.6.2 for every signal 1-31; a
# text outside this exact table refuses instead of being matched loosely.
_DARWIN_STRSIGNAL = {
    1: "Hangup",
    2: "Interrupt",
    3: "Quit",
    4: "Illegal instruction",
    5: "Trace/BPT trap",
    6: "Abort trap",
    7: "EMT trap",
    8: "Floating point exception",
    9: "Killed",
    10: "Bus error",
    11: "Segmentation fault",
    12: "Bad system call",
    13: "Broken pipe",
    14: "Alarm clock",
    15: "Terminated",
    16: "Urgent I/O condition",
    17: "Suspended (signal)",
    18: "Suspended",
    19: "Continued",
    20: "Child exited",
    21: "Stopped (tty input)",
    22: "Stopped (tty output)",
    23: "I/O possible",
    24: "Cputime limit exceeded",
    25: "Filesize limit exceeded",
    26: "Virtual timer expired",
    27: "Profiling timer expired",
    28: "Window size changes",
    29: "Information request",
    30: "User defined signal 1",
    31: "User defined signal 2",
}
_SIGNALS = {f"{text}: {number}": number for number, text in _DARWIN_STRSIGNAL.items()}
_PENDING_SPAWN = "\tstate = spawn scheduled"


class LaunchdFormatError(RuntimeError):
    """The output is outside the supported contract; native custody is retained."""


class LaunchdPendingSpawnError(RuntimeError):
    """launchd holds a spawn it has not performed (for example a missing program).

    It may still start the job later, so a pending spawn is live custody, never
    a terminal or absent job.
    """


class LaunchdJob(Record):
    """Typed facts from one supported ``launchctl print`` observation."""

    path: str
    program: str
    arguments: tuple[str, ...]
    working_directory: str
    stdout_path: str
    stderr_path: str
    uid: int = Field(ge=0)
    asid: int = Field(ge=0)
    umask: str
    exit_timeout: int = Field(ge=0)
    runs: int = Field(ge=0)
    state: Literal["running", "not running"]
    pid: int | None
    exit_code: int | None
    signal: int | None
    properties: tuple[str, ...]


_KNOWN = frozenset(_CRITICAL) | _DIAGNOSTIC | _OUTCOME


def _block(body: Iterator[str]) -> tuple[str, ...]:
    """One nested block; deeper nesting or a missing close refuses."""
    members: list[str] = []
    for member in body:
        if member == "\t}":
            return tuple(members)
        if not member.startswith("\t\t") or member.endswith(" = {"):
            raise LaunchdFormatError("launchd output has an unsupported nested block")
        members.append(member[2:])
    raise LaunchdFormatError("launchd output has an unterminated block")


def _field(entry: str, seen: set[str]) -> tuple[str, str]:
    key, separator, value = entry.partition(" = ")
    if not separator or key in seen:
        raise LaunchdFormatError(f"launchd output has an ambiguous field: {key!r}")
    if key not in _KNOWN:
        raise LaunchdFormatError(f"launchd output has an unsupported field: {key!r}")
    return key, value


def _body(text: str, target: str) -> Iterator[str]:
    lines = text.rstrip("\n").split("\n")
    if len(lines) < 3 or lines[0] != f"{target} = {{" or lines[-1] != "}":
        raise LaunchdFormatError("launchd output does not describe exactly the requested job")
    return iter(lines[1:-1])


def _split(text: str, target: str) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """Top-level fields and blocks, each unique and from the measured vocabulary."""
    scalars: dict[str, str] = {}
    blocks: dict[str, tuple[str, ...]] = {}
    body = _body(text, target)
    for line in body:
        if not line:
            continue
        if not line.startswith("\t") or line.startswith("\t\t"):
            raise LaunchdFormatError("launchd output has an unsupported indentation")
        entry = line[1:]
        seen = scalars.keys() | blocks.keys()
        if not entry.endswith(" = {"):
            key, value = _field(entry, seen)
            scalars[key] = value
            continue
        name = entry.removesuffix(" = {")
        if name not in _BLOCKS or name in seen:
            raise LaunchdFormatError(f"launchd output has an unexpected block: {name!r}")
        blocks[name] = _block(body)
    missing = [key for key in _CRITICAL if key not in scalars]
    if missing or "arguments" not in blocks:
        raise LaunchdFormatError(f"launchd output lacks critical job facts: {missing}")
    return scalars, blocks


def _integer(value: str) -> int:
    if re.fullmatch(r"\d+", value) is None:
        raise LaunchdFormatError(f"launchd field is not a decimal integer: {value!r}")
    return int(value)


def _running_pid(scalars: dict[str, str]) -> int:
    """A running job has exactly one owner PID and no exit fact yet."""
    facts = (
        _integer(scalars["active count"]),
        scalars.get("last exit code"),
        scalars.get("last terminating signal"),
    )
    if facts != (1, "(never exited)", None) or "pid" not in scalars:
        raise LaunchdFormatError("running launchd job facts are inconsistent")
    pid = _integer(scalars["pid"])
    if pid <= 1:
        raise LaunchdFormatError("running launchd job has no process")
    return pid


def _terminal_exit(scalars: dict[str, str]) -> tuple[int | None, int | None]:
    """A terminal job has no PID and exactly one exit code xor terminating signal."""
    code, signal = scalars.get("last exit code"), scalars.get("last terminating signal")
    if (
        _integer(scalars["active count"]) != 0
        or "pid" in scalars
        or (code is None) == (signal is None)
    ):
        raise LaunchdFormatError("terminal launchd job facts are inconsistent")
    if code is not None:
        match = _EXIT_CODE.fullmatch(code)
        if match is None:
            raise LaunchdFormatError(f"unsupported launchd exit code: {code!r}")
        return int(match[1]), None
    number = _SIGNALS.get(signal or "")
    if number is None:
        raise LaunchdFormatError(f"unsupported launchd terminating signal: {signal!r}")
    return None, number


def read_job(text: str, target: str) -> LaunchdJob:
    """Parse one exact job target; any unknown or conflicting fact refuses."""
    lines = list(_body(text, target))
    if _PENDING_SPAWN in lines:
        # Measured with a missing program: runs 1, launchd's own EX_CONFIG and
        # event triggers that start the job once the program appears.
        raise LaunchdPendingSpawnError(
            f"launchd holds a pending spawn of {target}; it may still start, so custody is retained"
        )
    scalars, blocks = _split(text, target)
    state = scalars["state"]
    if scalars["type"] != "LaunchAgent" or state not in {"running", "not running"}:
        raise LaunchdFormatError(f"unsupported launchd job type/state: {state!r}")
    domain = _DOMAIN.fullmatch(scalars["domain"])
    if domain is None or domain[2] != scalars["asid"]:
        raise LaunchdFormatError("launchd job domain/session facts are inconsistent")
    pid, exit_code, signal = None, None, None
    if state == "running":
        pid = _running_pid(scalars)
    else:
        exit_code, signal = _terminal_exit(scalars)
    return LaunchdJob(
        path=scalars["path"],
        program=scalars["program"],
        arguments=blocks["arguments"],
        working_directory=scalars["working directory"],
        stdout_path=scalars["stdout path"],
        stderr_path=scalars["stderr path"],
        uid=int(domain[1]),
        asid=_integer(scalars["asid"]),
        umask=scalars["umask"],
        exit_timeout=_integer(scalars["exit timeout"]),
        runs=_integer(scalars["runs"]),
        state="running" if state == "running" else "not running",
        pid=pid,
        exit_code=exit_code,
        signal=signal,
        properties=tuple(scalars["properties"].split(" | ")),
    )


def read_domain_asid(text: str, uid: int) -> int:
    """The audit session of the login domain ``gui/<uid>``, from its security context.

    Only the domain header is read: exactly one top-level ``security context``
    block holding exactly this uid and one decimal asid. Anything else refuses.
    """
    lines = text.rstrip("\n").split("\n")
    if len(lines) < 3 or lines[0] != f"gui/{uid} = {{" or lines[-1] != "}":
        raise LaunchdFormatError("launchd output does not describe exactly the login domain")
    starts = [index for index, line in enumerate(lines) if line == "\tsecurity context = {"]
    if len(starts) != 1:
        raise LaunchdFormatError("launchd login domain lacks one security context")
    block = lines[starts[0] + 1 : starts[0] + 4]
    match = re.fullmatch(r"\t\tasid = (\d+)", block[1]) if len(block) == 3 else None
    if match is None or block[0] != f"\t\tuid = {uid}" or block[2] != "\t}":
        raise LaunchdFormatError("launchd login domain security context is unsupported")
    return int(match[1])
