"""Settings-free, read-only native scheduler queries with explicit uncertainty.

Raw definitions remain private inputs, never response/log fields. Native command
failure is not absence — the one positive absence route is the definition file's
own lstat ENOENT plus a stable GUI enumeration. launchctl diagnostic text is
deliberately not parsed.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import re
import shlex
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from shared.managed_writer_barrier import Digest


class NativeReadUnavailableError(RuntimeError):
    """Unsupported, unreadable, expired or drifting native observation."""


class LauncherObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    definition: Literal["match", "mismatch", "absent", "unknown"] = "unknown"
    declared_home: Literal["match", "mismatch", "unknown"] = "unknown"
    declared_image: Literal["prepared", "other", "unknown"] = "unknown"
    loaded: bool | None = None
    enabled: bool | None = None
    # A disk declaration is not the scheduler's loaded argv or effective state.
    loaded_image: Literal["unknown"] = "unknown"
    # Digest of the bytes actually on disk when they could be read (the prepared
    # digest on match, the foreign digest on mismatch); None while unknown or
    # absent. A summary only: mismatch bytes are never parsed.
    current_digest: Digest | None = None


def native_read(argv: tuple[str, ...], valid_until: datetime) -> subprocess.CompletedProcess[bytes]:
    remaining = (valid_until - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise NativeReadUnavailableError("native observation deadline expired")
    try:
        result = subprocess.run(  # noqa: S603 — fixed read-only commands, no shell
            argv,
            capture_output=True,
            check=False,
            timeout=min(5.0, remaining),
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeReadUnavailableError("native scheduler query unavailable") from exc
    if max(len(result.stdout), len(result.stderr)) > 1024 * 1024:
        raise NativeReadUnavailableError("native scheduler response exceeds budget")
    if datetime.now(UTC) >= valid_until:
        raise NativeReadUnavailableError("native scheduler query expired")
    return result


def read_crontab(valid_until: datetime) -> bytes:
    """Read the current user's table; only the native no-table result is empty."""
    if sys.platform != "linux":
        raise NativeReadUnavailableError("crontab observation is Linux-only")
    result = native_read(("/usr/bin/crontab", "-l"), valid_until)
    if result.returncode == 0:
        return result.stdout
    # Do not accept permission/configuration failures merely containing a keyword.
    if (
        result.returncode == 1
        and not result.stdout
        and re.fullmatch(rb"no crontab for [A-Za-z0-9_.-]+\n?", result.stderr)
    ):
        return b""
    raise NativeReadUnavailableError("crontab could not be read")


def read_launchd_definition(label: str) -> bytes | None:
    """Read the exact definition bytes; None is a positively absent file.

    Absence requires the canonical directory and an lstat ENOENT for exactly
    this label — never an unreadable or odd path (those still refuse). Callers
    needing a stability check read twice and compare the two results.
    """
    if sys.platform != "darwin" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", label):
        raise NativeReadUnavailableError("unsupported launchd label/platform")
    directory = Path.home() / "Library" / "LaunchAgents"
    path = directory / f"{label}.plist"
    if directory.resolve() != directory:
        raise NativeReadUnavailableError("launchd directory is not canonical")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
        raise NativeReadUnavailableError("launchd definition is not a bounded regular file")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            info.st_dev,
            info.st_ino,
        ):
            raise NativeReadUnavailableError("launchd definition changed while opening")
        body = stream.read(1024 * 1024 + 1)
    if len(body) > 1024 * 1024:
        raise NativeReadUnavailableError("launchd definition grew beyond budget")
    return body


def launchd_loaded(label: str, valid_until: datetime) -> bool | None:
    """Exact `launchctl print` probe: True is the only positive it can return.

    This never returns False — None means "no verdict", NOT "not loaded".
    Failure could mean a missing GUI domain, privilege failure or absent job;
    diagnostic text is not an API (launchctl(1)), so absence must come from the
    stable GUI enumeration in `launchd_loaded_state`.
    """
    if sys.platform != "darwin" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", label):
        raise NativeReadUnavailableError("unsupported launchd label/platform")
    result = native_read(("/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"), valid_until)
    return True if result.returncode == 0 else None


def launchd_loaded_state(label: str, valid_until: datetime) -> bool | None:
    """The single three-valued loaded verdict for one label.

    True: the exact lookup answered. False: the lookup failed AND two equal,
    stable GUI enumerations exclude the label — the only legal absence route.
    None: everything else (an unreadable or drifting enumeration, or a label
    the enumeration lists while its exact lookup failed). None is unknown:
    callers must never read it as "not loaded".
    """
    if launchd_loaded(label, valid_until) is True:
        return True
    try:
        before = read_launchd_labels(valid_until)
        after = read_launchd_labels(valid_until)
    except NativeReadUnavailableError:
        return None
    if before != after or label in after:
        return None
    return False


def parse_launchd_labels(body: bytes) -> frozenset[str]:
    """Parse only launchctl list's documented three columns, not print output."""
    lines = body.decode("utf-8").splitlines()
    if not lines or lines[0].split() != ["PID", "Status", "Label"]:
        raise NativeReadUnavailableError("launchctl list format is unsupported")
    labels: set[str] = set()
    for line in lines[1:]:
        fields = line.split()
        if len(fields) != 3:
            raise NativeReadUnavailableError("launchctl list row is unsupported")
        pid, status, label = fields
        if (pid != "-" and not pid.isdecimal()) or not re.fullmatch(r"-?[0-9]+", status):
            raise NativeReadUnavailableError("launchctl list state is unsupported")
        if label in labels or any(ord(character) < 32 for character in label):
            raise NativeReadUnavailableError("launchctl list label is inconsistent")
        labels.add(label)
    return frozenset(labels)


def read_launchd_labels(valid_until: datetime) -> frozenset[str]:
    """Current GUI-domain enumeration; a background domain cannot prove absence."""
    if sys.platform != "darwin":
        raise NativeReadUnavailableError("launchd enumeration is macOS-only")
    uid = native_read(("/bin/launchctl", "manageruid"), valid_until)
    name = native_read(("/bin/launchctl", "managername"), valid_until)
    if (
        uid.returncode
        or name.returncode
        or uid.stdout.strip() != str(os.getuid()).encode()
        or name.stdout.strip() != b"Aqua"
    ):
        raise NativeReadUnavailableError("launchctl is not in this user's GUI domain")
    result = native_read(("/bin/launchctl", "list"), valid_until)
    if result.returncode:
        raise NativeReadUnavailableError("launchctl list is unreadable")
    return parse_launchd_labels(result.stdout)


def declaration_binding(
    home: Path, artifact_digest: str, declared_home: object, argv: object
) -> dict[str, str]:
    if declared_home != str(home):
        return {"declared_home": "mismatch"}
    if not isinstance(argv, list) or not argv:
        return {"declared_home": "match"}
    arguments = cast(list[object], argv)
    executable_text = arguments[0]
    if not isinstance(executable_text, str) or not all(isinstance(arg, str) for arg in arguments):
        return {"declared_home": "match"}
    executable = Path(executable_text)
    if not executable.is_absolute():
        return {"declared_home": "match"}
    prepared = home / "releases" / artifact_digest / "venv"
    try:
        inside = executable.resolve(strict=True).is_relative_to(prepared)
    except (OSError, RuntimeError):
        return {"declared_home": "match"}
    return {"declared_home": "match", "declared_image": "prepared" if inside else "other"}


def _loads_in_aqua(value: object) -> bool:
    """Whether a LimitLoadToSessionType declaration can load in the Aqua domain.

    An absent declaration means the default session types (Aqua included). An
    explicit declaration must cover "Aqua" — anything else, including unknown
    shapes, refuses to unknown rather than pretending observability.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value == "Aqua"
    if isinstance(value, list):
        return any(item == "Aqua" for item in cast("list[object]", value))
    return False


def observe_launchd(
    label: str, digest: str, home: Path, artifact_digest: str, valid_until: datetime
) -> LauncherObservation:
    first = read_launchd_definition(label)
    loaded = launchd_loaded_state(label, valid_until)
    second = read_launchd_definition(label)
    after_loaded = launchd_loaded_state(label, valid_until)
    # Both facts must be identical across the two reads: a loaded verdict that
    # differs in any way (including a None that can hide a changed enumeration)
    # refuses, never falling back to the first read.
    if first != second or loaded != after_loaded:
        raise NativeReadUnavailableError("launchd state changed during observation")
    if first is None:
        # A positively absent definition still carries the loaded facts the
        # fence derivation pairs with "removed" — never as unknown.
        return LauncherObservation(definition="absent", loaded=loaded, current_digest=None)
    actual_digest = hashlib.sha256(first).hexdigest()
    if actual_digest != digest:
        # Summary only: the foreign bytes are reported by digest, never parsed.
        return LauncherObservation(
            definition="mismatch", loaded=loaded, current_digest=actual_digest
        )
    parsed = plistlib.loads(first)
    if not isinstance(parsed, dict):
        raise NativeReadUnavailableError("launchd definition is not a dictionary")
    definition = cast(dict[str, object], parsed)
    if definition.get("Label") != label:
        raise NativeReadUnavailableError("launchd definition label is inconsistent")
    # A job that cannot load in this Aqua domain is unobservable here: an
    # explicit non-Aqua session limit degrades to unknown, not a match.
    if not _loads_in_aqua(definition.get("LimitLoadToSessionType")):
        return LauncherObservation()
    raw_environment = definition.get("EnvironmentVariables")
    if not isinstance(raw_environment, dict):
        raise NativeReadUnavailableError("launchd definition has no explicit environment")
    environment = cast(dict[str, object], raw_environment)
    # Program can override argv[0]; refuse contradictory declarations.
    argv = definition.get("ProgramArguments")
    if "Program" in definition and (
        not isinstance(argv, list)
        or not argv
        or definition["Program"] != cast(list[object], argv)[0]
    ):
        raise NativeReadUnavailableError("launchd executable declarations disagree")
    return LauncherObservation.model_validate(
        {
            "definition": "match",
            "loaded": loaded,
            "current_digest": digest,
            **declaration_binding(
                home, artifact_digest, environment.get("AVA_HOME"), cast(object, argv)
            ),
        }
    )


def observe_crontab(
    name: str, digest: str, home: Path, artifact_digest: str, valid_until: datetime
) -> LauncherObservation:
    if name != digest or not re.fullmatch(r"[0-9a-f]{64}", name):
        raise NativeReadUnavailableError("crontab identity must be its exact definition digest")
    first = read_crontab(valid_until)
    second = read_crontab(valid_until)
    if first != second:
        raise NativeReadUnavailableError("crontab changed during observation")
    lines = first.decode("utf-8").splitlines()
    matches = [line for line in lines if hashlib.sha256(line.encode()).hexdigest() == name]
    if not matches:
        return LauncherObservation(definition="absent", enabled=False)
    if len(matches) != 1 or hashlib.sha256(matches[0].encode()).hexdigest() != digest:
        return LauncherObservation(definition="mismatch")
    line = matches[0]
    if any(character in line for character in "`$;|&<>\\%(){}"):
        raise NativeReadUnavailableError("crontab shell expression is unsupported")
    fields = shlex.split(line, comments=True)
    offset = 1 if fields and fields[0] == "@reboot" else 5
    command = fields[offset:]
    if not command or not command[0].startswith("AVA_HOME="):
        raise NativeReadUnavailableError("crontab lacks explicit command home")
    return LauncherObservation.model_validate(
        {
            "definition": "match",
            "enabled": True,
            **declaration_binding(
                home, artifact_digest, command[0][len("AVA_HOME=") :], command[1:]
            ),
        }
    )
