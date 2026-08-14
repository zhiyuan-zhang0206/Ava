"""Composer commands endpoint — `GET /api/commands`.

Lists the registered prompt templates (see `ava._commands`) for the web
Composer's `/`-autocomplete. Read-only filesystem scan; no auth beyond the
gateway's default session gate. Only the metadata the dropdown needs is
returned — the body is never sent to the browser, since expansion happens
server-side in the agent's claim node (`ava._commands.expand_command`).
"""

from __future__ import annotations

from fastapi import APIRouter

from ava._commands import discover_commands
from gateway.schemas import CommandItem

router = APIRouter()


@router.get("/api/commands")
def get_commands() -> list[CommandItem]:
    """All composer commands, deduped by name and sorted — name + description +
    instruction-hint only (the dropdown's needs)."""
    return [
        CommandItem(
            name=c["name"],
            description=c["description"],
            instruction_hint=c["instruction_hint"],
        )
        for c in discover_commands()
    ]
