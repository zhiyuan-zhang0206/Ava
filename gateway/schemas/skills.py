"""Skills panel response models — GET /api/skills.

A single-host, gateway-local read: this host's `$AVA_HOME/skills/` load dir
correlated with the install registry (`installed.json`). Skills are per-machine
(the registry is machine-local; there is no cluster-shared row), so unlike the
cross-machine plugin/MCP inventory this is not a `?machine=` matrix.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

SkillLayer = Literal["core", "plugin", "machine", "untracked"]
"""Where a skill's content comes from — the registry `origin`, surfaced:

- "core" — a repo skill (`<repo>/ava_builtins/skills/<name>/`), converge-managed.
- "plugin" — bundled by a plugin (`plugins/<p>/skills/`), converge-managed.
- "machine" — user-installed / hand-registered on this machine (`ava skill
  register` / `ava plugins install <git-url>`); converge never touches it.
- "untracked" — a directory in the load dir with no registry entry: present on
  disk but the skill scanner will not load it.
"""


class SkillView(BaseModel):
    """One skill in the load dir, correlated with its registry entry.

    `modified_locally` is the converge "was hand-edited" signal — the on-disk
    copy's tree hash no longer matches the `content_hash` converge last wrote
    (always False for machine/untracked entries, which carry no managed hash).
    `origin_path` is the source tree a converge-managed copy derives from.
    """

    name: str
    layer: SkillLayer
    enabled: bool
    modified_locally: bool
    origin_path: str | None = None


class SkillsView(BaseModel):
    """Every top-level directory in this host's skills load dir."""

    skills: list[SkillView]


class SkillEnableUpdate(BaseModel):
    """Request body for PUT /api/skills — toggle one skill's enabled flag."""

    name: str
    enabled: bool
