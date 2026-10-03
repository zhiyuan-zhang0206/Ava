"""Shared base for the per-domain config sub-models.

Every sub-model is its own `BaseSettings` so it populates from the flat
`os.environ` (loaded from `$AVA_HOME/.env` by `load_ava_env`) through each
field's env alias — the split into domains is invisible to `.env`. The aggregate
in `base/config/__init__.py` holds one instance of each.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from base.host.env.dotenv_boot import resolve_ava_home


def _unit_home() -> Path:
    """Default data-root for path fields whose default_factory runs at
    sub-model construction: the process's home (`resolve_ava_home`), so pidfiles /
    memory / logs default under THIS unit's home."""
    return resolve_ava_home()


class EnvSettings(BaseSettings):
    """Base for every config sub-model.

    - extra="ignore": one domain's model must not choke on another domain's (or a
      third party's) env vars.
    - populate_by_name=True: fields carry an env alias (their AVA_* key), but the
      per-agent config overlay and its round-trip validation construct/validate by
      field NAME (`settings.model_dump()` keys) — allow both.
    """

    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)
