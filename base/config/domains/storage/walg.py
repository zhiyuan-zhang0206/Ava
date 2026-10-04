"""WAL-G config — WalgSettings.

One key switches the physical-backup engine (`services/backup/walg/`) on:
the path of a 0600 JSON file in WAL-G's own configuration format
(`WALG_OSS_PREFIX`, `OSS_*`, `WALG_LIBSODIUM_KEY_PATH`, ...). Ava does not
translate that format into typed settings, so there is no second copy of any
credential in `.env` and no field to keep in step with WAL-G. Unset (the
default) every WAL-G code path is a no-op: Postgres gets no archive arguments,
converge installs nothing, the health probe has nothing to check.

Aggregated by base/config.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from base.config.base import EnvSettings


class WalgSettings(EnvSettings):
    walg_config_file: Path | None = Field(
        default=None,
        alias="AVA_WALG_CONFIG_FILE",
        description=(
            "Path of the owner-only (0600) JSON file holding WAL-G's own configuration "
            "(WALG_OSS_PREFIX, OSS_ACCESS_KEY_ID/SECRET, OSS_ENDPOINT, OSS_REGION, "
            "WALG_LIBSODIUM_KEY_PATH, WALG_LIBSODIUM_KEY_TRANSFORM=hex, "
            "WALG_PREVENT_WAL_OVERWRITE=true). Setting it turns WAL archiving on: Postgres "
            "is started with archive_mode=on and an archive_command that calls the pinned "
            "wal-g with this file. Postgres reads archive_mode only at launch, so a change "
            "takes effect after `ava stop` + `ava start`; unset it and restart to turn "
            "archiving off. Unset (the default) disables everything WAL-G."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
            "bootstrap": False,
        },
    )
