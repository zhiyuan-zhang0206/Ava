"""Named per-agent config-overlay templates (model, plugin fields, other
settings)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ava import _gateway_client as _client
from ava._gateway_client import PresetNotFoundError as PresetNotFoundError
from ava._sdk_validation import coerce_str


@dataclass
class Preset:
    id: int
    name: str
    label: str
    description: str | None
    config: dict[str, object]
    created_at: datetime
    updated_at: datetime


def list() -> list[Preset]:  # pyright: ignore[reportGeneralTypeIssues] — `list` shadows builtin; safe at runtime via `from __future__ import annotations`
    """Return every preset, ordered by name."""
    rows = _client.list_presets()
    return [
        Preset(
            id=r["id"],
            name=r["name"],
            label=r["label"],
            description=r.get("description"),
            config=r["config"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


def get(name: str) -> Preset:
    """Return the preset whose name matches exactly."""
    row = _client.get_preset(coerce_str(name, "name"))
    return Preset(
        id=row["id"],
        name=row["name"],
        label=row["label"],
        description=row.get("description"),
        config=row["config"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


__all_for_ava__ = ["Preset", "PresetNotFoundError", "get", "list"]
