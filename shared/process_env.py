"""Small process-environment primitives for one-shot child protocols.

Runtime configuration belongs in ``shared.config``. These helpers are only for
process mechanics that Settings cannot represent: copying the complete live
environment across ``exec``, consuming a one-shot marker, and adopting values a
child durably committed for clients created later in the surviving parent.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


def inherited_process_env(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy the live environment and apply explicit child-only overrides."""
    child = dict(os.environ)
    if overrides is not None:
        child.update(overrides)
    return child


def consume_process_marker(name: str, *, armed_value: str) -> bool:
    """Remove a one-shot marker and report whether it advertises this protocol."""
    return os.environ.pop(name, None) == armed_value


def update_process_env(values: Mapping[str, str]) -> None:
    """Adopt committed handoff values for clients built later in this process."""
    os.environ.update(values)


def remove_process_env(names: tuple[str, ...]) -> None:
    """Strip parent-only authority before a restricted child starts work."""

    for name in names:
        os.environ.pop(name, None)


def restricted_process_env() -> dict[str, str]:
    """Build the fixed environment for a child that must inherit no authority."""

    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "TZ": "UTC",
    }


_PROXY_ENV_NAMES = (
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
)


def forwarded_proxy_env() -> dict[str, str]:
    """Forward the host's proxy variables to a child that inherits no authority.

    A restricted child still needs the host's egress path: on hosts whose
    resolver maps public names into a fake-IP range (a transparent proxy such
    as Clash on WSL), a direct connect is blackholed and only the env proxy
    reaches the destination — the 2026-09-13 activation lost every restore
    worker to TCP SYN timeouts this way. Only proxy variable names cross;
    values are copied verbatim and case-preserving, and an empty value is
    dropped rather than forwarded as an empty override, so a host without
    proxies forwards nothing and composing this is a no-op.
    """

    forwarded: dict[str, str] = {}
    for name in _PROXY_ENV_NAMES:
        value = os.environ.get(name)
        if value:
            forwarded[name] = value
    return forwarded
