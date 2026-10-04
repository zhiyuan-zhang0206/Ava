"""The placement units under `services/`: a service package, or a module beside the groups.

`services/<group>/<service>/` directories hold service packages and carry no `__init__.py`: the
unit of a module in a group is its service package, not the group. A package or module directly
under `services/` (`services/redis_bridge/`, `services/pidfile.py`) is a unit of its own.
"""

from __future__ import annotations

from pathlib import Path

SERVICE_GROUPS = frozenset(
    {"agent_runner", "backup", "derived", "desktop", "entrypoints", "supervision", "upkeep", "wake"}
)


def unit_of(parts: list[str]) -> str:
    """The unit of dotted module `parts` (starting `services`, at least two long)."""
    depth = 3 if parts[1] in SERVICE_GROUPS and len(parts) > 2 else 2
    return ".".join(parts[:depth])


def build(services: Path) -> set[str]:
    """Every unit below an existing `services/` directory (docs layers and dunder names excluded)."""
    units: set[str] = set()
    for child in services.iterdir() if services.is_dir() else ():
        if child.name == "docs" or child.name.startswith("__"):
            continue
        if child.name in SERVICE_GROUPS:
            units.update(
                f"services.{child.name}.{member.stem}"
                for member in child.iterdir()
                if member.name not in {"docs", "tests"}
                and (member.is_dir() or member.suffix == ".py")
                and not member.name.startswith("__")
            )
        elif child.is_dir() or child.suffix == ".py":
            units.add(f"services.{child.stem}")
    return units
