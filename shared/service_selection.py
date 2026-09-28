"""The start lifecycle's durable desired service set.

An omitted selection retains the prior intent. An allowlist remains an allowlist
when new plugins appear; --all-services explicitly resets to the complete roster.
Transient internal omissions never change the desired state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from shared.paths import ava_home
from shared.private_storage import write_private_bytes


@dataclass(frozen=True)
class ServiceSelection:
    mode: str
    names: frozenset[str]

    def enabled(self, name: str) -> bool:
        return name in self.names if self.mode == "only" else name not in self.names


def selection_path() -> Path:
    return ava_home() / "service-selection.json"


def read_selection() -> ServiceSelection:
    path = selection_path()
    if not path.exists():
        return ServiceSelection("except", frozenset())
    data = json.loads(path.read_text())
    if set(data) != {"version", "mode", "names"} or data["version"] != 1:
        raise RuntimeError("invalid service selection")
    if data["mode"] not in {"only", "except"} or not isinstance(data["names"], list):
        raise RuntimeError("invalid service selection")
    if any(not isinstance(n, str) or not n for n in data["names"]):
        raise RuntimeError("invalid service selection names")
    return ServiceSelection(data["mode"], frozenset(data["names"]))


def resolve_selection(
    available: set[str],
    *,
    only: tuple[str, ...] = (),
    excluded: tuple[str, ...] = (),
    all_services: bool = False,
    persist: bool = True,
    publish: bool = True,
) -> set[str]:
    _validate_request(available, only, excluded, all_services=all_services)
    current = read_selection()
    if not persist:
        if only or all_services:
            raise ValueError("an internal transient launch cannot change desired services")
        return {n for n in available if not current.enabled(n)} | set(excluded)
    if only or excluded or all_services:
        current = ServiceSelection("only" if only else "except", frozenset(only or excluded))
        if publish:
            _write_selection(current)
    return {n for n in available if not current.enabled(n)}


def _validate_request(
    available: set[str], only: tuple[str, ...], excluded: tuple[str, ...], *, all_services: bool
) -> None:
    if sum((bool(only), bool(excluded), all_services)) > 1:
        raise ValueError("only-service, disable-service and all-services are mutually exclusive")
    requested = set(only) | set(excluded)
    if requested - available:
        raise ValueError("unknown service selection: " + ", ".join(sorted(requested - available)))


def _write_selection(selection: ServiceSelection) -> None:
    payload = (
        json.dumps({"version": 1, "mode": selection.mode, "names": sorted(selection.names)}) + "\n"
    )
    path = selection_path()
    if not path.exists() or path.read_text() != payload:
        write_private_bytes(path, payload.encode())
