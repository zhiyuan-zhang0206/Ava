"""What the data-plane commands read from configuration, as one immutable value.

The composition roots are the process entries: the `meiva start`, `meiva stop` and `meiva status`
steps in `cli/commands/lifecycle` and the pooler converge step. They read the process
configuration once, through `data_plane_config_from_settings`, and everything below them receives
the result as `config`, so a test hands a function the exact values it wants with
`dataclasses.replace` instead of patching `settings`.

Two middle functions read in place where no test controls the values: `prepare_cold_start`
builds the value for the start step's calls, and `root_driver.tree_manifest` reads only
`is_remote` for the launch environments (`service_extra_env` below it takes the bool). They are
not roots; a function that needs a different value takes `config`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from base.config import ConfigBoot, settings
from base.host.net.url_secret import names_foreign_host

__all__ = ["DataPlaneConfig", "data_plane_config_from_boot", "data_plane_config_from_settings"]


@dataclass(frozen=True)
class DataPlaneConfig:
    """The `data_plane` and `services` fields these commands read."""

    # The URLs carry the login and the two passwords are secrets: none of them reaches a
    # repr, so a log line or a traceback that formats the config never prints them.
    db_url: str = field(repr=False)
    redis_url: str = field(repr=False)
    redis_admin_password: str = field(repr=False)
    cluster_secret: str = field(repr=False)
    pgbouncer_enabled: bool
    redis_bin_dir: str
    trusted_cidrs: str
    events_channel: str
    memory_search_backend: str
    embedding_backend: str

    @property
    def is_remote(self) -> bool:
        """Whether the data plane is hosted off-box: derived from the URLs, never stored, so a
        copy with a swapped URL answers for that URL (same rule as the settings' own `is_remote`)."""
        return names_foreign_host(self.db_url, self.redis_url)


def data_plane_config_from_settings() -> DataPlaneConfig:
    """The config as the process settings hold it now: the composition roots call this once."""
    return _from_view(settings)


def data_plane_config_from_boot(boot: ConfigBoot) -> DataPlaneConfig:
    """The same config read from one explicit configuration owner (tests build theirs this way)."""
    return _from_view(boot.view)


def _from_view(view: Any) -> DataPlaneConfig:
    plane = view.data_plane
    return DataPlaneConfig(
        db_url=plane.db_url,
        redis_url=plane.redis_url,
        redis_admin_password=plane.redis_admin_password,
        cluster_secret=plane.cluster_secret,
        pgbouncer_enabled=plane.pgbouncer_enabled,
        redis_bin_dir=plane.redis_bin_dir,
        trusted_cidrs=plane.trusted_cidrs,
        events_channel=plane.events_channel,
        memory_search_backend=view.services.memory_search_backend,
        embedding_backend=view.services.embedding_backend,
    )
