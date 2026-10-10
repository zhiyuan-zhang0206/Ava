"""The database dial's configuration: one frozen value, read from settings in one place.

`DbConfig` holds exactly what dialing the cluster Postgres decides on. A composition root
builds it (`db_config_from_settings()`, or `Database.from_settings()` in `base.db.handle`) and
hands components the `Database` handle, never the URL: the URL carries the login.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from base.config import ConfigBoot, settings


@dataclass(frozen=True)
class DbConfig:
    """What `connect()` / `pool()` / `async_pool()` read to decide where and how to dial."""

    # The cluster's one access URL (PgBouncer when pooling is on); carries the login.
    db_url: str = field(repr=False)
    db_sslmode: str
    db_pool_min_size: int
    db_pool_max_size: int
    # Whether the access URL names a pooler, which the direct (admin-plane) dial must bypass.
    pgbouncer_enabled: bool
    # The authority result that was delivered with this connection slice.
    db_authority_refusal: str | None = field(default=None, repr=False)


def db_config_from_settings() -> DbConfig:
    """Build the slice from the live settings (read at each call, so a test or an overlay that
    changed a field reaches the next dial exactly as the former direct reads did)."""
    return _from_view(settings)


def db_config_from_boot(boot: ConfigBoot) -> DbConfig:
    """Read the same dial slice from this composition root's configuration owner."""
    return _from_view(boot.view)


def _from_view(view: Any) -> DbConfig:
    return DbConfig(
        db_url=view.data_plane.db_url,
        db_sslmode=view.data_plane.db_sslmode,
        db_pool_min_size=view.data_plane.db_pool_min_size,
        db_pool_max_size=view.data_plane.db_pool_max_size,
        pgbouncer_enabled=view.data_plane.pgbouncer_enabled,
        db_authority_refusal=view.env_boot.db_authority_refusal,
    )
