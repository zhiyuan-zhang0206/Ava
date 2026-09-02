"""Settings-free, read-only native scheduler queries with explicit uncertainty.

Raw definitions remain private inputs, never response/log fields. Native command
failure is not absence. launchctl diagnostic text is deliberately not parsed.
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


def read_launchd_definition(label: str) -> bytes:
    if sys.platform != "darwin" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", label):
        raise NativeReadUnavailableError("unsupported launchd label/platform")
    directory = Path.home() / "Library" / "LaunchAgents"
    path = directory / f"{label}.plist"
    if directory.resolve() != directory:
        raise NativeReadUnavailableError("launchd directory is not canonical")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
        raise NativeReadUnavailableError("launchd definition is not a bounded regular file")
    return path.read_bytes()


def launchd_loaded(label: str, valid_until: datetime) -> bool | None:
    if sys.platform != "darwin" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", label):
        raise NativeReadUnavailableError("unsupported launchd label/platform")
    result = native_read(("/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"), valid_until)
    # Failure could mean a missing GUI domain, privilege failure or absent job.
    # Diagnostic text is not an API (launchctl(1)); never infer absence from it.
    return True if result.returncode == 0 else None


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


def observe_launchd(
    label: str, digest: str, home: Path, artifact_digest: str, valid_until: datetime
) -> LauncherObservation:
    first = read_launchd_definition(label)
    loaded = launchd_loaded(label, valid_until)
    # Positive absence needs the matching GUI enumeration, not a failed print.
    if loaded is None:
        try:
            before_labels = read_launchd_labels(valid_until)
            after_labels = read_launchd_labels(valid_until)
            loaded = label in after_labels if before_labels == after_labels else None
        except NativeReadUnavailableError:
            loaded = None
    second = read_launchd_definition(label)
    after_loaded = launchd_loaded(label, valid_until)
    if (
        first != second
        or (loaded is True and after_loaded is not True)
        or (loaded is False and after_loaded is True)
    ):
        raise NativeReadUnavailableError("launchd state changed during observation")
    if hashlib.sha256(first).hexdigest() != digest:
        return LauncherObservation(definition="mismatch")
    parsed = plistlib.loads(first)
    if not isinstance(parsed, dict):
        raise NativeReadUnavailableError("launchd definition is not a dictionary")
    definition = cast(dict[str, object], parsed)
    if definition.get("Label") != label:
        raise NativeReadUnavailableError("launchd definition label is inconsistent")
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
            **declaration_binding(home, artifact_digest, environment.get("AVA_HOME"), argv),
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
