"""Converge step: install and validate WAL-G when AVA_WALG_CONFIG_FILE switches it on.

Runs before Postgres starts (right after its runtime step), so a broken
configuration fails `ava start` here instead of becoming a Postgres whose archive
command can never succeed. With the key unset every part of this is a no-op: a
development home installs nothing and registers nothing.
"""

from __future__ import annotations

from base.cluster.dataplane.walg_binary import ensure_walg_binary
from base.config import settings
from base.host.private_storage import ensure_private_dir
from cli.commands.converge.spec import ConvergeCtx
from services.backup.walg import config as walg_config


def converge_walg(ctx: ConvergeCtx) -> None:
    """Install the pinned binary, validate the configuration, pin the key on first use.

    Raises:
        RuntimeError: the data plane is remote-managed, this platform has no pinned
            wal-g, the download failed its checksum, or the configuration is unusable
            (`WalgConfigError` names the file and the setting, never a value).
    """
    if not walg_config.enabled(path_reader=lambda: ctx.read_config().view.walg.walg_config_file):
        return
    if settings.data_plane.is_remote:
        raise RuntimeError(
            "AVA_WALG_CONFIG_FILE is set but this cluster's Postgres is remote-managed: "
            "WAL archiving needs the locally launched Postgres (unset the key)"
        )
    ensure_walg_binary()
    walg_config.load_walg_config(path_reader=lambda: ctx.read_config().view.walg.walg_config_file)
    ensure_private_dir(ctx.ava_home / "backups" / "walg")
