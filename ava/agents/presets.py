"""Named per-agent config-overlay templates (model, plugin fields, other
settings)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ava import gateway_client
from ava.sdk_surface.validation import coerce_str


@dataclass
class Preset:
    id: int
    name: str
    label: str
    description: str | None
    config: dict[str, object]
    created_at: datetime
    updated_at: datetime


class PresetNotFoundError(Exception):
    """No preset with that name exists."""


def _from_row(row: dict[str, Any]) -> Preset:
    return Preset(
        id=row["id"],
        name=row["name"],
        label=row["label"],
        description=row["description"],
        config=row["config"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def list() -> list[Preset]:  # pyright: ignore[reportGeneralTypeIssues] — `list` shadows builtin; safe at runtime via `from __future__ import annotations`
    """Return every preset, ordered by name."""
    return [_from_row(row) for row in gateway_client.list_presets()]


def get(name: str) -> Preset:
    """Return the preset whose name matches exactly."""
    name = coerce_str(name, "name")
    # The full list is small (a handful of presets), so filter it here rather
    # than keep a get-by-name gateway endpoint.
    for row in gateway_client.list_presets():
        if row["name"] == name:
            return _from_row(row)
    raise PresetNotFoundError(f"preset {name!r} not found")


__all_for_ava__ = ["Preset", "PresetNotFoundError", "get", "list"]
