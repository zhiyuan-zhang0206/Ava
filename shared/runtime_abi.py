"""The stable release ABI tag: what a retained image requires of its host.

A prepared image runs its own retained interpreter, dependency wheels and
native libraries. Those bind to the operating system, the CPU architecture, the
C library (glibc on Linux) or macOS release they were prepared against, and the
interpreter's Python ABI. They do not bind to the kernel release or the OS
patch level that `platform.platform()` also spells out, so that string is
recorded in the manifest as provenance only and is never compared.

`current_abi()` reads the facts of the calling interpreter on this host, fresh
on every call because a reboot can change the host. Preparation runs it inside
the image's own interpreter to record the tag. Every verifier runs it in its own
process to observe the host it is on now. The verifier is itself a retained image
or the source checkout on that host, so the Python ABI comparison keeps one
Python ABI per host. `abi_refusal()` holds the compatibility rules:

- `os`, `arch`, `libc`, `python` and `abiflags` are equal;
- Linux: the host glibc is at least the image's floor, which is the glibc the
  image was prepared against;
- macOS: the host major release is at least the image's major.

The macOS deployment target is the image interpreter's own build minimum. A
valid tag keeps it at or below the recorded major, so the major is the binding
floor; the host tag's deployment target describes only the verifying
interpreter and is not compared. A kernel, OS patch or minor update, and a glibc
or macOS major upgrade keep an image bootable. An architecture or Python ABI
change, or a host older than the image's floor, needs a re-prepared image. An
unknown operating system, C library, interpreter or malformed field refuses;
nothing is guessed.

Standard library only: the bare release-store contract imports this module
through `shared.deploy.release.runtime_release`.
"""

from __future__ import annotations

import os
import platform
import re
import sys
import sysconfig
from dataclasses import asdict, dataclass
from typing import cast

_FIELDS = (
    "os",
    "arch",
    "libc",
    "libc_version",
    "macos",
    "macos_deployment_target",
    "python",
    "abiflags",
)
_ARCH = re.compile(r"[a-z0-9_]+")
_PYTHON = re.compile(r"cpython-[0-9]+")
_ABIFLAGS = re.compile(r"[a-z]*")
_GLIBC_FACT = re.compile(r"glibc ([0-9]+\.[0-9]+)")
_GLIBC_VERSION = re.compile(r"[0-9]+\.[0-9]+")
_MACOS_MAJOR = re.compile(r"[1-9][0-9]*")
_DEPLOYMENT_TARGET = re.compile(r"([1-9][0-9]*)(?:\.[0-9]+){0,2}")
# Before 11, the macOS major was the second component; the compatibility shim
# also reports 10.16 to old-SDK binaries. Neither is a supported image host.
_MIN_MACOS = 11


class AbiTagError(ValueError):
    """A malformed, unsupported or unknown ABI fact."""


@dataclass(frozen=True)
class AbiTag:
    """One interpreter on one host; construction validates every field."""

    os: str
    arch: str
    libc: str | None
    libc_version: str | None
    macos: str | None
    macos_deployment_target: str | None
    python: str
    abiflags: str

    def __post_init__(self) -> None:
        if _ARCH.fullmatch(self.arch) is None:
            raise AbiTagError(f"unsupported ABI architecture {self.arch!r}")
        if _PYTHON.fullmatch(self.python) is None or _ABIFLAGS.fullmatch(self.abiflags) is None:
            raise AbiTagError(f"unsupported Python ABI {self.python!r}/{self.abiflags!r}")
        if self.os == "linux":
            _require_linux(self)
        elif self.os == "macos":
            _require_macos(self)
        else:
            raise AbiTagError(f"unsupported ABI operating system {self.os!r}")

    def floor(self) -> tuple[int, ...]:
        """The host-release floor: glibc (major, minor) on Linux, (major,) on macOS."""
        text = cast(str, self.libc_version if self.os == "linux" else self.macos)
        return tuple(int(part) for part in text.split("."))

    def to_json(self) -> dict[str, str | None]:
        return asdict(self)

    def __str__(self) -> str:
        floor = f"glibc{self.libc_version}" if self.os == "linux" else self.macos
        return f"{self.os}-{self.arch}-{floor}-{self.python}{self.abiflags}"


def _require_linux(tag: AbiTag) -> None:
    if (
        tag.libc != "glibc"
        or tag.libc_version is None
        or _GLIBC_VERSION.fullmatch(tag.libc_version) is None
        or tag.macos is not None
        or tag.macos_deployment_target is not None
    ):
        raise AbiTagError("a Linux ABI tag names glibc and its version floor, and no macOS")


def _require_macos(tag: AbiTag) -> None:
    if (
        tag.libc is not None
        or tag.libc_version is not None
        or tag.macos is None
        or _MACOS_MAJOR.fullmatch(tag.macos) is None
        or int(tag.macos) < _MIN_MACOS
    ):
        raise AbiTagError(f"a macOS ABI tag names a major release >= {_MIN_MACOS} and no libc")
    target = tag.macos_deployment_target
    if target is not None:
        match = _DEPLOYMENT_TARGET.fullmatch(target)
        if match is None or int(match[1]) > int(tag.macos):
            raise AbiTagError(f"macOS deployment target {target!r} exceeds the tagged major")


def _optional_text(fields: dict[str, object], name: str) -> str | None:
    value = fields[name]
    if value is not None and not isinstance(value, str):
        raise AbiTagError(f"ABI field {name} must be a string or null")
    return value


def _text(fields: dict[str, object], name: str) -> str:
    value = _optional_text(fields, name)
    if value is None:
        raise AbiTagError(f"ABI field {name} is required")
    return value


def parse_abi_tag(value: object) -> AbiTag:
    """Read the manifest form: exactly the known fields, each a string or null."""
    if not isinstance(value, dict):
        raise AbiTagError("abi_tag must be an object")
    fields = cast(dict[str, object], value)
    if set(fields) != set(_FIELDS):
        raise AbiTagError(f"abi_tag fields must be exactly {sorted(_FIELDS)}")
    return AbiTag(
        os=_text(fields, "os"),
        arch=_text(fields, "arch"),
        libc=_optional_text(fields, "libc"),
        libc_version=_optional_text(fields, "libc_version"),
        macos=_optional_text(fields, "macos"),
        macos_deployment_target=_optional_text(fields, "macos_deployment_target"),
        python=_text(fields, "python"),
        abiflags=_text(fields, "abiflags"),
    )


def abi_from_facts(
    *,
    system: str,
    machine: str,
    glibc: str | None,
    mac_release: str,
    deployment_target: object,
    cache_tag: str | None,
    abiflags: str,
) -> AbiTag:
    """Normalize raw interpreter facts; `current_abi()` supplies the real ones.

    `system` is `sys.platform`, `glibc` the `CS_GNU_LIBC_VERSION` string (for
    example "glibc 2.39"), `mac_release` `platform.mac_ver()[0]`, and
    `deployment_target` the interpreter's `MACOSX_DEPLOYMENT_TARGET`.
    """
    if cache_tag is None:
        raise AbiTagError("interpreter has no implementation cache tag")
    if system == "linux":
        match = None if glibc is None else _GLIBC_FACT.fullmatch(glibc)
        if match is None:
            raise AbiTagError(f"unsupported Linux C library {glibc!r}; images require glibc")
        return AbiTag("linux", machine.lower(), "glibc", match[1], None, None, cache_tag, abiflags)
    if system == "darwin":
        if deployment_target is not None and not isinstance(deployment_target, str):
            raise AbiTagError(f"unreadable macOS deployment target {deployment_target!r}")
        major = mac_release.partition(".")[0]
        target = deployment_target or None
        return AbiTag("macos", machine.lower(), None, None, major, target, cache_tag, abiflags)
    raise AbiTagError(f"no release ABI for platform {system!r}")


def _glibc() -> str | None:
    try:
        return os.confstr("CS_GNU_LIBC_VERSION")
    except (ValueError, OSError):  # A non-glibc C library does not know the name.
        return None


def current_abi() -> AbiTag:
    """This interpreter on this host, read fresh on every call."""
    if sys.platform not in {"linux", "darwin"}:
        raise AbiTagError(f"no release ABI for platform {sys.platform!r}")
    return abi_from_facts(
        system=sys.platform,
        machine=platform.machine(),
        glibc=_glibc() if sys.platform == "linux" else None,
        mac_release=platform.mac_ver()[0],
        deployment_target=sysconfig.get_config_var("MACOSX_DEPLOYMENT_TARGET"),
        cache_tag=sys.implementation.cache_tag,
        abiflags=sys.abiflags,
    )


def abi_refusal(image: AbiTag, host: AbiTag) -> str | None:
    """Why `host` cannot run an image tagged `image`, or None when it can."""
    for name in ("os", "arch", "libc", "python", "abiflags"):
        if getattr(image, name) != getattr(host, name):
            return (
                f"{name} {getattr(image, name)!r} differs from the host's {getattr(host, name)!r}"
            )
    if host.floor() < image.floor():
        kind = "glibc" if image.os == "linux" else "macOS"
        found, needed = (".".join(map(str, tag.floor())) for tag in (host, image))
        return f"host {kind} {found} is older than the image floor {needed}"
    return None
