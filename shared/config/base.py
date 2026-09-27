"""Shared base for the per-domain config sub-models.

Every sub-model is its own `BaseSettings` so it populates from the flat
`os.environ` (loaded from `$AVA_HOME/.env` by `load_ava_env`) through each
field's env alias — the split into domains is invisible to `.env`. The aggregate
in `shared/config/__init__.py` holds one instance of each.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _unit_home() -> Path:
    """Default data-root for path fields whose default_factory cannot reference
    the ava_home field (pydantic resolves fields independently at init). Mirrors
    the AVA_HOME resolution (env AVA_HOME, else ~/.ava) so pidfiles / memory /
    milvus-data / logs default under THIS unit's home and follow a
    ~/.ava -> ~/.ava_gateway rename instead of pinning to ~/.ava."""
    home = os.environ.get("AVA_HOME")
    return (Path(home) if home else Path.home() / ".ava").expanduser()


class EnvSettings(BaseSettings):
    """Base for every config sub-model.

    - extra="ignore": one domain's model must not choke on another domain's (or a
      third party's) env vars.
    - populate_by_name=True: fields carry an env alias (their AVA_* key), but the
      per-agent config overlay and its round-trip validation construct/validate by
      field NAME (`settings.model_dump()` keys) — allow both.
    """

    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)
